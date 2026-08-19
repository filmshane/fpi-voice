"""FPI voice API — Retell plumbing for Alex + Lisa checklist handoff."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import crm, elevenlabs_dispatch, elevenlabs_webhook, retell_dispatch, retell_tools
from app.config import Settings, get_settings
from app.handoff_checklist import DEFAULT_REDIAL_COOLDOWN_MINUTES, evaluate_handoff_checklist
from app.retell_dispatch import to_e164

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fpi.voice")

app = FastAPI(title="FPI Voice / Alex", version="1.2.0")


@app.on_event("startup")
def _startup() -> None:
    s = get_settings()
    crm.ensure_retell_columns(s.crm_db_path)
    crm.ensure_elevenlabs_columns(s.crm_db_path)
    log.info(
        "FPI voice up elevenlabs_webhook_secret=%s crm=%s",
        bool(s.elevenlabs_webhook_secret),
        s.crm_db_path,
    )


def _sync_web_crm() -> None:
    try:
        import subprocess

        subprocess.run(
            ["python3", "/home/shanem/FPI-Corp/CRM/sync_crm_to_web.py"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def _check_secret(settings: Settings, authorization: str | None, x_fpi_secret: str | None) -> None:
    secret = (settings.dispatch_secret or "").strip()
    if not secret:
        return
    auth = (authorization or "").removeprefix("Bearer ").strip()
    if auth == secret or (x_fpi_secret or "").strip() == secret:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "ok": True,
        "service": "fpi-voice",
        "company": s.company_name,
        "retell": {
            "api_key_configured": bool(s.retell_api_key),
            "agent_id": s.retell_agent_id,
            "from_number_configured": bool(s.retell_from_number),
            "create_call": f"{s.retell_api_base}/v2/create-phone-call",
            "create_web_call": f"{s.retell_api_base}/v2/create-web-call",
            "required_dynamic_variables": list(retell_dispatch.AGENT_REQUIRED_DYN_VARS),
            "webhook": "SCRAPPED — use elevenlabs post-call webhook",
        },
        "elevenlabs": {
            "api_key_configured": bool(s.elevenlabs_api_key),
            "agent_id": s.elevenlabs_agent_id or None,
            "agent_phone_number_id_configured": bool(s.elevenlabs_agent_phone_number_id),
            "webhook_secret_configured": bool(s.elevenlabs_webhook_secret),
            "signature_enforced": bool(s.elevenlabs_webhook_enforce_signature),
            "outbound_call": "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
            "post_call_webhook": f"{s.public_base_url.rstrip('/')}/api/voice/alex-elevenlabs-webhook",
            "preview_open_call": "/api/voice/elevenlabs/open-call/preview/{lead_id}",
            "dispatch_open_call": "/api/voice/elevenlabs/open-call/{lead_id}",
        },
        "crm_db": s.crm_db_path,
        "llm_model": s.llm_model,
        "public_base_url": s.public_base_url,
        "paths": {
            "dispatch": "/api/voice/dispatch/{lead_id}",
            "checklist": "/api/voice/lisa/checklist/{lead_id}",
            "handoff": "/api/voice/lisa/handoff/{lead_id}",
            "elevenlabs_webhook": "/api/voice/alex-elevenlabs-webhook",
            "elevenlabs_webhook_aliases": [
                "/api/voice/elevenlabs/post-call",
                "/api/voice/webhooks/elevenlabs/post-call",
            ],
            "elevenlabs_open_call_preview": "/api/voice/elevenlabs/open-call/preview/{lead_id}",
            "elevenlabs_open_call": "/api/voice/elevenlabs/open-call/{lead_id}",
            "get_call": "/api/voice/calls/{call_id}",
            "create_web_call": "/api/voice/web-call/{lead_id}",
            "preview_web_call": "/api/voice/web-call/preview/{lead_id}",
            "tools": {
                "lookup_lead": "/api/voice/tools/lookup_lead",
                "crm_upsert_lead": "/api/voice/tools/crm_upsert_lead",
                "crm_log_activity": "/api/voice/tools/crm_log_activity",
                "calendar_book_ryan": "/api/voice/tools/calendar_book_ryan",
                "suppress_lead": "/api/voice/tools/suppress_lead",
            },
        },
    }


@app.get("/api/voice/leads")
async def voice_leads() -> dict[str, Any]:
    s = get_settings()
    return {"ok": True, "leads": crm.list_leads(s.crm_db_path)}


@app.get("/api/voice/lisa/checklist/{lead_id}")
async def lisa_checklist(lead_id: str) -> dict[str, Any]:
    """Evaluate Lisa → Alex handoff checklist without dialing."""
    s = get_settings()
    lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")
    report = evaluate_handoff_checklist(lead)
    return {"ok": True, "lead_id": lead_id, "checklist": report}


@app.post("/api/voice/lisa/handoff/{lead_id}")
async def lisa_handoff(
    lead_id: str,
    payload: dict[str, Any] | None = None,
    authorization: str | None = Header(default=None),
    x_fpi_secret: str | None = Header(default=None),
) -> JSONResponse:
    """
    Lisa complete-checklist handoff:
      1) optional CRM field updates from body
      2) set status APPROVED_LEAD_SENDING_ALEX
      3) run checklist
      4) if ready → Retell create-phone-call
      5) on success → status CURR_ALEX
    """
    s = get_settings()
    _check_secret(s, authorization, x_fpi_secret)
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    force = bool(payload.get("force"))
    cooldown = int(payload.get("redial_cooldown_minutes") or DEFAULT_REDIAL_COOLDOWN_MINUTES)

    skip = {"dry_run", "force", "redial_cooldown_minutes", "dispatch", "mark_approved", "phone", "address"}
    fields = {k: v for k, v in payload.items() if k not in skip}

    if "ai_call_consent" in fields:
        fields["ai_call_consent"] = (
            1 if fields["ai_call_consent"] in (True, 1, "1", "Y", "yes", "true") else 0
        )
    if "website_opt_in" in fields:
        fields["website_opt_in"] = (
            1 if fields["website_opt_in"] in (True, 1, "1", "Y", "yes", "true") else 0
        )
    if payload.get("phone") and not fields.get("phone_primary"):
        e = to_e164(str(payload.get("phone")))
        if e:
            fields["phone_primary"] = e
            fields["phones_json"] = json.dumps([e])
    if payload.get("address") and not fields.get("property_address"):
        fields["property_address"] = payload["address"]

    if payload.get("mark_approved", True):
        fields.setdefault("status", "APPROVED_LEAD_SENDING_ALEX")
        fields.setdefault("owner_agent", "lisa")

    crm.ensure_retell_columns(s.crm_db_path)
    if fields:
        lead = crm.upsert_lead_fields(s.crm_db_path, lead_id, fields, actor="lisa")
    else:
        lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    report = evaluate_handoff_checklist(lead, redial_cooldown_minutes=cooldown)
    crm.upsert_lead_fields(
        s.crm_db_path,
        lead_id,
        {
            "lisa_checklist_json": json.dumps(report),
            "lisa_checklist_at": crm.utcnow(),
        },
        actor="lisa",
    )
    # reload lead after checklist stamp
    lead = crm.get_lead(s.crm_db_path, lead_id) or lead

    if dry_run:
        return JSONResponse(
            {
                "ok": report["ready"],
                "dry_run": True,
                "lead_id": lead_id,
                "checklist": report,
                "would_dispatch": report["ready"] or force,
            }
        )

    if not report["ready"] and not force:
        return JSONResponse(
            {
                "ok": False,
                "error": "checklist_failed",
                "lead_id": lead_id,
                "checklist": report,
            },
            status_code=400,
        )

    if str(lead.get("status") or "").upper() != "APPROVED_LEAD_SENDING_ALEX":
        lead = crm.upsert_lead_fields(
            s.crm_db_path,
            lead_id,
            {"status": "APPROVED_LEAD_SENDING_ALEX", "owner_agent": "lisa"},
            actor="lisa",
        )

    result = await retell_dispatch.create_phone_call(s, lead=lead)
    if result.get("ok"):
        call_id = str(result.get("call_id") or "")
        crm.mark_call(
            s.crm_db_path,
            lead_id,
            call_id=call_id,
            status="CURR_ALEX",
            extra={"last_retell_event": "dispatch_outbound", "owner_agent": "alex"},
        )
        crm.add_activity(
            s.crm_db_path,
            lead_id,
            "lisa_handoff_retell",
            f"Lisa checklist OK → Alex call {call_id}",
            actor="lisa",
            payload={"call_id": call_id, "to": result.get("to_number"), "checklist_ready": True},
        )
        _sync_web_crm()
        return JSONResponse(
            {
                "ok": True,
                "lead_id": lead_id,
                "status": "CURR_ALEX",
                "checklist": report,
                "retell": result,
            }
        )

    crm.add_activity(
        s.crm_db_path,
        lead_id,
        "lisa_handoff_failed",
        str(result.get("error") or "retell_failed")[:500],
        actor="lisa",
        payload=result,
    )
    return JSONResponse(
        {"ok": False, "lead_id": lead_id, "checklist": report, "retell": result},
        status_code=502,
    )


@app.post("/api/voice/dispatch/{lead_id}")
async def dispatch_lead(
    lead_id: str,
    authorization: str | None = Header(default=None),
    x_fpi_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Place outbound Retell call for CRM lead (Alex). Enforces Lisa checklist."""
    s = get_settings()
    _check_secret(s, authorization, x_fpi_secret)

    lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    report = evaluate_handoff_checklist(lead)
    if not report["ready"]:
        return JSONResponse(
            {"ok": False, "error": "checklist_failed", "checklist": report},
            status_code=400,
        )

    result = await retell_dispatch.create_phone_call(s, lead=lead)
    if result.get("ok"):
        call_id = str(result.get("call_id") or "")
        crm.mark_call(
            s.crm_db_path,
            lead_id,
            call_id=call_id,
            status="CURR_ALEX",
            extra={"last_retell_event": "dispatch_outbound", "owner_agent": "alex"},
        )
        crm.add_activity(
            s.crm_db_path,
            lead_id,
            "retell_dispatch",
            f"Outbound Alex call started call_id={call_id}",
            payload={"call_id": call_id, "to": result.get("to_number")},
        )
        _sync_web_crm()
    return JSONResponse(
        {**result, "checklist": report} if isinstance(result, dict) else result,
        status_code=200 if result.get("ok") else 502,
    )


@app.get("/api/voice/elevenlabs/open-call/preview/{lead_id}")
async def elevenlabs_open_call_preview(lead_id: str) -> dict[str, Any]:
    """Show exact open-call JSON + headers + field catalog FPI sends to ElevenLabs."""
    s = get_settings()
    lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(404, f"lead not found: {lead_id}")
    return {
        "ok": True,
        "lead_id": lead_id,
        "open_call": elevenlabs_dispatch.preview_open_call(s, lead),
    }


@app.post("/api/voice/elevenlabs/open-call/{lead_id}")
async def elevenlabs_open_call(
    lead_id: str,
    authorization: str | None = Header(default=None),
    x_fpi_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Place ElevenLabs Twilio outbound call for CRM lead (Alex)."""
    s = get_settings()
    _check_secret(s, authorization, x_fpi_secret)
    lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(404, f"lead not found: {lead_id}")

    result = await elevenlabs_dispatch.create_outbound_call(s, lead=lead)
    if result.get("ok"):
        conv = str(result.get("conversation_id") or "")
        crm.mark_call(
            s.crm_db_path,
            lead_id,
            call_id=conv or None,
            status="CURR_ALEX",
            extra={
                "last_elevenlabs_event": "outbound_call_started",
                "elevenlabs_conversation_id": conv or None,
                "elevenlabs_agent_id": result.get("agent_id") or s.elevenlabs_agent_id,
                "owner_agent": "alex",
            },
        )
        crm.add_activity(
            s.crm_db_path,
            lead_id,
            "elevenlabs_outbound",
            f"Open call started conversation_id={conv}",
            payload={"conversation_id": conv, "to_number": result.get("to_number")},
            actor="alex",
        )
        _sync_web_crm()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/api/voice/alex-elevenlabs-webhook")
@app.post("/api/voice/elevenlabs/post-call")
@app.post("/api/voice/webhooks/elevenlabs/post-call")
async def elevenlabs_post_call_webhook(
    request: Request,
    elevenlabs_signature: str | None = Header(default=None, alias="elevenlabs-signature"),
    eleven_labs_signature: str | None = Header(default=None, alias="ElevenLabs-Signature"),
) -> JSONResponse:
    """ElevenLabs → FPI post-call webhooks (JSON body already structured).

    Types: post_call_transcription | post_call_audio | call_initiation_failure
    Security: HMAC via elevenlabs-signature / ElevenLabs-Signature header.
    """
    s = get_settings()
    raw = await request.body()
    sig = elevenlabs_signature or eleven_labs_signature

    enforce = bool(s.elevenlabs_webhook_enforce_signature)
    secret = (s.elevenlabs_webhook_secret or "").strip()

    if enforce:
        if not secret:
            log.error("elevenlabs webhook rejected: secret not configured")
            return JSONResponse(
                {"ok": False, "error": "webhook secret not configured"},
                status_code=503,
            )
        try:
            elevenlabs_webhook.verify_signature(raw, sig, secret)
        except elevenlabs_webhook.SignatureError as e:
            log.warning("elevenlabs signature failed: %s", e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=401)
    elif secret and sig:
        # Best-effort verify when not enforced but both present
        try:
            elevenlabs_webhook.verify_signature(raw, sig, secret)
        except elevenlabs_webhook.SignatureError as e:
            log.warning("elevenlabs signature failed (enforce off): %s", e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=401)
    elif not secret:
        log.warning("elevenlabs webhook accepted WITHOUT signature verification (no secret)")

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "json object required"}, status_code=400)

    try:
        out = elevenlabs_webhook.handle_payload(s.crm_db_path, payload)
    except Exception:
        log.exception("elevenlabs webhook handler failed")
        # Still 200 to avoid auto-disable after repeated 5xx — log + store failure note
        return JSONResponse(
            {"ok": False, "error": "handler_exception", "accepted": True},
            status_code=200,
        )

    if out.get("crm") == "updated":
        _sync_web_crm()
    return JSONResponse(out, status_code=200)


@app.post("/api/voice/alex-retell-webhook")
@app.post("/api/voice/retell-webhook")
async def retell_webhook_scrapped() -> JSONResponse:
    """Retell post-call webhook SCRAPPED — use ElevenLabs endpoint."""
    return JSONResponse(
        {
            "ok": False,
            "error": "retell_webhook_scrapped",
            "use": "/api/voice/alex-elevenlabs-webhook",
            "public": "https://firstpropertyinvestment.us/api/voice/alex-elevenlabs-webhook",
            "types": [
                "post_call_transcription",
                "post_call_audio",
                "call_initiation_failure",
            ],
        },
        status_code=410,
    )


@app.get("/api/voice/calls/{call_id}")
async def fetch_call(call_id: str) -> dict[str, Any]:
    s = get_settings()
    return await retell_dispatch.get_call(s, call_id)


@app.get("/api/voice/web-call/preview/{lead_id}")
async def preview_web_call(lead_id: str) -> dict[str, Any]:
    """Show the exact Create Web Call body (agent-required dyn vars as strings)."""
    s = get_settings()
    lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(404, f"lead not found: {lead_id}")
    body = retell_dispatch.preview_create_web_call_body(s, lead)
    return {"ok": True, "lead_id": lead_id, "retell_create_web_call_body": body}


@app.post("/api/voice/web-call/{lead_id}")
async def create_web_call_for_lead(
    lead_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_fpi_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Create Retell web call for CRM lead with required dynamic variables."""
    s = get_settings()
    _check_secret(s, authorization, x_fpi_secret)
    lead = crm.get_lead(s.crm_db_path, lead_id)
    if not lead:
        raise HTTPException(404, f"lead not found: {lead_id}")

    override = None
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            override = payload.get("crm_summary")
    except Exception:
        payload = {}

    result = await retell_dispatch.create_web_call(
        s,
        lead=lead,
        source_hint="website_web_call",
        crm_summary_override=str(override) if override else None,
    )
    if result.get("ok"):
        call_id = result.get("call_id")
        crm.mark_call(
            s.crm_db_path,
            lead_id,
            call_id=call_id,
            status="CURR_ALEX",
            extra={"last_retell_event": "web_call_created", "owner_agent": "alex"},
        )
        crm.add_activity(
            s.crm_db_path,
            lead_id,
            "retell_web_call",
            f"Web call created call_id={call_id}",
            payload={"call_id": call_id},
        )
        _sync_web_crm()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/api/voice/web-call")
async def create_web_call_inline(request: Request) -> JSONResponse:
    """Create web call from inline lead fields (website path).

    Accepts either lead_id (CRM lookup) or name/address/summary fields.
    """
    s = get_settings()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    lead_id = str(payload.get("lead_id") or "").strip()
    lead: dict[str, Any] | None = None
    if lead_id:
        lead = crm.get_lead(s.crm_db_path, lead_id)

    if not lead:
        # Build ephemeral lead dict from website form fields
        name = str(payload.get("customer_name") or payload.get("name") or "").strip()
        first = str(payload.get("first_name") or "").strip()
        last = str(payload.get("last_name") or "").strip()
        if name and not first:
            parts = name.split(None, 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else last
        lead = {
            "id": lead_id or str(payload.get("id") or ""),
            "first_name": first,
            "last_name": last,
            "full_name": name or f"{first} {last}".strip(),
            "property_address": str(
                payload.get("property_address") or payload.get("address") or ""
            ),
            "phone_primary": str(payload.get("phone") or payload.get("customer_phone") or ""),
            "email_primary": str(payload.get("email") or ""),
            "motivation": str(payload.get("motivation") or payload.get("message") or ""),
            "house_info_summary": str(payload.get("house_info_summary") or ""),
            "preferred_call_window": str(payload.get("call_preference") or ""),
            "source": str(payload.get("source") or "website_web_call"),
            "ai_call_consent": 1 if str(payload.get("ai_call_consent") or "").lower() in (
                "1", "yes", "y", "true"
            ) else payload.get("ai_call_consent"),
            "status": str(payload.get("status") or "WEBSITE_WEB_CALL"),
            "lisa_notes": str(payload.get("lisa_notes") or ""),
        }
        if not lead.get("id"):
            # stable-ish id from phone/address if present
            import re as _re
            import hashlib

            key = (lead.get("phone_primary") or "") + "|" + (lead.get("property_address") or name)
            slug = _re.sub(r"[^a-z0-9]+", "-", (lead.get("property_address") or name or "web").lower())[:40]
            lead["id"] = f"web-{slug}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"

    crm_summary_override = payload.get("crm_summary")
    result = await retell_dispatch.create_web_call(
        s,
        lead=lead,
        source_hint=str(payload.get("source") or "website_web_call"),
        crm_summary_override=str(crm_summary_override) if crm_summary_override else None,
    )
    # Persist if we have CRM and lead_id
    lid = str(lead.get("id") or "")
    if result.get("ok") and lid and crm.get_lead(s.crm_db_path, lid):
        crm.mark_call(
            s.crm_db_path,
            lid,
            call_id=result.get("call_id"),
            status="CURR_ALEX",
            extra={"last_retell_event": "web_call_created", "owner_agent": "alex"},
        )
        crm.add_activity(
            s.crm_db_path,
            lid,
            "retell_web_call",
            f"Web call created call_id={result.get('call_id')}",
            payload={"call_id": result.get("call_id")},
        )
        _sync_web_crm()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/api/voice/opt-out")
async def opt_out(payload: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    lead_id = str(payload.get("lead_id") or "")
    if not lead_id:
        raise HTTPException(400, "lead_id required")
    crm.mark_call(
        s.crm_db_path,
        lead_id,
        status="SUPPRESSED",
        extra={"dnc_flag": "Y", "stop_reason": str(payload.get("reason") or "voice_opt_out")},
    )
    crm.add_activity(s.crm_db_path, lead_id, "opt_out", str(payload.get("reason") or "opt_out"))
    return {"ok": True}


async def _tool_body(request: Request) -> dict[str, Any]:
    """Retell may send {args: {...}} or flat JSON."""
    try:
        data = await request.json()
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


@app.post("/api/voice/tools/lookup_lead")
async def tool_lookup_lead(request: Request) -> JSONResponse:
    s = get_settings()
    payload = await _tool_body(request)
    out = retell_tools.lookup_lead(s.crm_db_path, payload)
    return JSONResponse(out, status_code=200 if out.get("ok") else 404)


@app.post("/api/voice/tools/crm_upsert_lead")
async def tool_crm_upsert_lead(request: Request) -> JSONResponse:
    s = get_settings()
    payload = await _tool_body(request)
    out = retell_tools.crm_upsert_lead(s.crm_db_path, payload)
    if out.get("ok"):
        _sync_web_crm()
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.post("/api/voice/tools/crm_log_activity")
async def tool_crm_log_activity(request: Request) -> JSONResponse:
    s = get_settings()
    payload = await _tool_body(request)
    out = retell_tools.crm_log_activity(s.crm_db_path, payload)
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.post("/api/voice/tools/calendar_book_ryan")
async def tool_calendar_book_ryan(request: Request) -> JSONResponse:
    s = get_settings()
    payload = await _tool_body(request)
    out = retell_tools.calendar_book_ryan(s.crm_db_path, payload)
    if out.get("ok"):
        _sync_web_crm()
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.post("/api/voice/tools/suppress_lead")
async def tool_suppress_lead(request: Request) -> JSONResponse:
    s = get_settings()
    payload = await _tool_body(request)
    out = retell_tools.suppress_lead(s.crm_db_path, payload)
    if out.get("ok"):
        _sync_web_crm()
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=False)


if __name__ == "__main__":
    run()
