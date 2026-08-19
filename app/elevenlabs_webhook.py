"""ElevenLabs post-call webhooks → FPI CRM.

Handles:
  - post_call_transcription
  - post_call_audio
  - call_initiation_failure

Security: verify ElevenLabs-Signature (HMAC-SHA256), same scheme as
elevenlabs Python SDK construct_event:
  header: t=<unix>,v0=<hex>
  signed payload: f"{timestamp}.{raw_body}"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

from app import crm

log = logging.getLogger("fpi.elevenlabs")

WEBHOOK_DATA_DIR = Path("/opt/fpi-voice/data/elevenlabs-webhooks")
AUDIO_DIR = WEBHOOK_DATA_DIR / "audio"
TOLERANCE_SECS = 30 * 60  # 30 minutes, matches ElevenLabs SDK default


class SignatureError(ValueError):
    """Invalid or missing ElevenLabs-Signature."""


def verify_signature(raw_body: bytes, sig_header: str | None, secret: str) -> None:
    """Raise SignatureError if HMAC validation fails.

    Header format: ``t=<timestamp>,v0=<hex_digest>``
    Message: ``f"{timestamp}.{body_utf8}"``
    """
    if not secret:
        raise SignatureError("ELEVENLABS_WEBHOOK_SECRET not configured")
    if not sig_header or not str(sig_header).strip():
        raise SignatureError("Missing signature header")

    header = str(sig_header).strip()
    # Also accept lowercase header value already normalized by caller
    parts: dict[str, str] = {}
    for piece in header.split(","):
        piece = piece.strip()
        if "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        parts[k.strip()] = v.strip()

    timestamp = parts.get("t")
    # signature may be "v0=hex" already split so key is v0
    sig_hash = parts.get("v0")
    if not timestamp or not sig_hash:
        raise SignatureError("No signature hash found with expected scheme v0")

    try:
        ts = int(timestamp)
    except ValueError as e:
        raise SignatureError("Invalid timestamp") from e

    if abs(time.time() - ts) > TOLERANCE_SECS:
        raise SignatureError("Timestamp outside tolerance window")

    body_text = raw_body.decode("utf-8")
    message = f"{timestamp}.{body_text}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_hash):
        raise SignatureError("Signature mismatch")


def _extract_lead_id(data: dict[str, Any]) -> str:
    """Pull lead_id from dynamic variables / client data / metadata."""
    # conversation_initiation_client_data.dynamic_variables.lead_id
    client = data.get("conversation_initiation_client_data") or {}
    if isinstance(client, dict):
        dyn = client.get("dynamic_variables") or {}
        if isinstance(dyn, dict):
            for key in ("lead_id", "leadId", "crm_lead_id"):
                if dyn.get(key):
                    return str(dyn[key]).strip()
    # top-level metadata
    meta = data.get("metadata") or {}
    if isinstance(meta, dict):
        for key in ("lead_id", "leadId"):
            if meta.get(key):
                return str(meta[key]).strip()
        # nested body (failure payloads)
        body = meta.get("body") or {}
        if isinstance(body, dict) and body.get("lead_id"):
            return str(body["lead_id"]).strip()
    if data.get("lead_id"):
        return str(data["lead_id"]).strip()
    return ""


def _extract_phone(data: dict[str, Any]) -> str:
    meta = data.get("metadata") or {}
    if not isinstance(meta, dict):
        return ""
    body = meta.get("body") if isinstance(meta.get("body"), dict) else {}
    for key in ("to_number", "To", "Called", "phone", "phone_primary"):
        if isinstance(body, dict) and body.get(key):
            return str(body[key])
        if meta.get(key):
            return str(meta[key])
    client = data.get("conversation_initiation_client_data") or {}
    if isinstance(client, dict):
        dyn = client.get("dynamic_variables") or {}
        if isinstance(dyn, dict) and dyn.get("phone_primary"):
            return str(dyn["phone_primary"])
    return ""


def _resolve_lead(db_path: str, data: dict[str, Any]) -> dict[str, Any] | None:
    lead_id = _extract_lead_id(data)
    if lead_id:
        lead = crm.get_lead(db_path, lead_id)
        if lead:
            return lead
    phone = _extract_phone(data)
    if phone:
        # reuse tool-style lookup via phone digits
        from app.retell_dispatch import to_e164

        e164 = to_e164(phone) or phone
        try:
            import sqlite3

            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM leads WHERE phone_primary = ? OR phones_json LIKE ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (e164, f"%{e164}%"),
            ).fetchone()
            con.close()
            if row:
                return dict(row)
        except Exception:
            log.exception("phone lead lookup failed")
    # conversation_id match
    conv = str(data.get("conversation_id") or "")
    if conv:
        try:
            import sqlite3

            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM leads WHERE elevenlabs_conversation_id = ? "
                "OR retell_call_id = ? ORDER BY updated_at DESC LIMIT 1",
                (conv, conv),
            ).fetchone()
            con.close()
            if row:
                return dict(row)
        except Exception:
            pass
    return None


def _save_raw(event_type: str, conversation_id: str, payload: dict[str, Any]) -> Path:
    WEBHOOK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_type = "".join(c if c.isalnum() or c in "-_" else "_" for c in event_type)[:40]
    safe_cid = "".join(c if c.isalnum() or c in "-_" else "_" for c in (conversation_id or "unknown"))[:64]
    ts = int(time.time())
    path = WEBHOOK_DATA_DIR / f"{ts}_{safe_type}_{safe_cid}.json"
    # Strip huge audio before saving JSON twin (audio saved separately)
    clone = dict(payload)
    data = clone.get("data")
    if isinstance(data, dict) and data.get("full_audio"):
        data = dict(data)
        data["full_audio"] = f"<omitted base64 len={len(str(payload['data'].get('full_audio') or ''))}>"
        clone["data"] = data
    path.write_text(json.dumps(clone, indent=2, default=str) + "\n")
    return path


def _save_audio(conversation_id: str, b64: str) -> Path | None:
    if not b64:
        return None
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    safe_cid = "".join(c if c.isalnum() or c in "-_" else "_" for c in conversation_id)[:64] or "unknown"
    path = AUDIO_DIR / f"{safe_cid}.mp3"
    try:
        path.write_bytes(base64.b64decode(b64))
        return path
    except Exception:
        log.exception("audio decode failed conversation_id=%s", conversation_id)
        return None


def _qualified_from_analysis(analysis: dict[str, Any]) -> str | None:
    if not isinstance(analysis, dict):
        return None
    # data_collection_results may hold custom fields
    dcr = analysis.get("data_collection_results") or {}
    if isinstance(dcr, dict):
        for key in ("qualified", "is_qualified", "Qualified"):
            node = dcr.get(key)
            if isinstance(node, dict) and "value" in node:
                v = node.get("value")
            else:
                v = node
            if v is not None:
                return "Y" if str(v).lower() in ("y", "yes", "true", "1", "success") else "N"
    cs = str(analysis.get("call_successful") or "").lower()
    if cs in ("success", "successful", "true", "yes"):
        return None  # call completed — not the same as seller qualified
    if cs in ("failure", "failed", "false", "no"):
        return None
    return None


def handle_payload(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Process verified ElevenLabs webhook JSON. Always safe to return 200 body."""
    event_type = str(payload.get("type") or "")
    event_ts = payload.get("event_timestamp")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    conversation_id = str(data.get("conversation_id") or "")
    agent_id = str(data.get("agent_id") or "")

    raw_path = _save_raw(event_type or "unknown", conversation_id, payload)
    lead = _resolve_lead(db_path, data) if data else None
    lead_id = (lead or {}).get("id") or _extract_lead_id(data) or ""

    log.info(
        "elevenlabs webhook type=%s conversation_id=%s lead_id=%s agent_id=%s",
        event_type,
        conversation_id,
        lead_id or "-",
        agent_id or "-",
    )

    result: dict[str, Any] = {
        "ok": True,
        "type": event_type or "unknown",
        "conversation_id": conversation_id or None,
        "lead_id": lead_id or None,
        "saved": str(raw_path),
        "event_timestamp": event_ts,
    }

    crm.ensure_elevenlabs_columns(db_path)

    if event_type == "post_call_transcription":
        analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
        summary = str(analysis.get("transcript_summary") or "")[:4000]
        status_raw = str(data.get("status") or "")
        qualified = _qualified_from_analysis(analysis)
        # Optional evaluation: if data collection has motivation etc., fold into notes
        notes_parts = [p for p in [summary, f"11labs status={status_raw}" if status_raw else ""] if p]
        notes = " | ".join(notes_parts)[:4000]

        new_status = None
        if qualified == "Y":
            new_status = "SCOUTING_LEAD"
        elif qualified == "N":
            new_status = "DISQUALIFIED"
        else:
            new_status = "ALEX_MANAGING"

        if lead_id and crm.get_lead(db_path, lead_id):
            crm.mark_call(
                db_path,
                lead_id,
                call_id=conversation_id or None,
                status=new_status,
                qualified=qualified,
                alex_notes=notes or None,
                extra={
                    "last_elevenlabs_event": event_type,
                    "elevenlabs_conversation_id": conversation_id or None,
                    "elevenlabs_agent_id": agent_id or None,
                    "owner_agent": "alex",
                },
            )
            crm.add_activity(
                db_path,
                lead_id,
                "elevenlabs_webhook",
                f"{event_type} qualified={qualified} status={status_raw}",
                payload={
                    "conversation_id": conversation_id,
                    "type": event_type,
                    "call_successful": analysis.get("call_successful"),
                    "raw_path": str(raw_path),
                },
                actor="elevenlabs",
            )
            result["crm"] = "updated"
            result["status"] = new_status
            result["qualified"] = qualified
        else:
            result["crm"] = "no_lead_match"
            log.warning("elevenlabs transcription no lead match conversation_id=%s", conversation_id)

    elif event_type == "post_call_audio":
        audio_path = _save_audio(conversation_id, str(data.get("full_audio") or ""))
        result["audio_path"] = str(audio_path) if audio_path else None
        if lead_id and crm.get_lead(db_path, lead_id):
            crm.add_activity(
                db_path,
                lead_id,
                "elevenlabs_webhook",
                f"post_call_audio saved={bool(audio_path)}",
                payload={
                    "conversation_id": conversation_id,
                    "type": event_type,
                    "audio_path": str(audio_path) if audio_path else None,
                },
                actor="elevenlabs",
            )
            if conversation_id:
                crm.mark_call(
                    db_path,
                    lead_id,
                    call_id=conversation_id,
                    extra={
                        "last_elevenlabs_event": event_type,
                        "elevenlabs_conversation_id": conversation_id,
                        "elevenlabs_audio_path": str(audio_path) if audio_path else None,
                    },
                )
            result["crm"] = "updated"
        else:
            result["crm"] = "no_lead_match"

    elif event_type == "call_initiation_failure":
        failure_reason = str(data.get("failure_reason") or "unknown")
        if lead_id and crm.get_lead(db_path, lead_id):
            crm.mark_call(
                db_path,
                lead_id,
                call_id=conversation_id or None,
                status="ALEX_CALL_FAILED",
                alex_notes=f"ElevenLabs call_initiation_failure: {failure_reason}"[:2000],
                extra={
                    "last_elevenlabs_event": event_type,
                    "elevenlabs_conversation_id": conversation_id or None,
                    "elevenlabs_failure_reason": failure_reason,
                    "owner_agent": "alex",
                },
            )
            crm.add_activity(
                db_path,
                lead_id,
                "elevenlabs_webhook",
                f"call_initiation_failure reason={failure_reason}",
                payload={
                    "conversation_id": conversation_id,
                    "type": event_type,
                    "failure_reason": failure_reason,
                    "metadata": data.get("metadata"),
                },
                actor="elevenlabs",
            )
            result["crm"] = "updated"
            result["failure_reason"] = failure_reason
        else:
            result["crm"] = "no_lead_match"
            log.warning(
                "elevenlabs failure no lead match conversation_id=%s reason=%s",
                conversation_id,
                failure_reason,
            )
    else:
        result["crm"] = "ignored_unknown_type"
        log.info("elevenlabs unknown webhook type=%s", event_type)

    return result
