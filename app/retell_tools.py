"""Retell custom function / webhook tools → FPI CRM."""
from __future__ import annotations

import json
import re
from typing import Any

from app import crm
from app.retell_dispatch import to_e164


def _digits(phone: str | None) -> str:
    return re.sub(r"\D+", "", phone or "")


def find_lead(db_path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve lead by lead_id, phone, email, or retell call_id."""
    lead_id = str(
        payload.get("lead_id")
        or payload.get("leadId")
        or (payload.get("args") or {}).get("lead_id")
        or ""
    ).strip()
    if lead_id:
        hit = crm.get_lead(db_path, lead_id)
        if hit:
            return hit

    phone = (
        payload.get("phone")
        or payload.get("customer_phone")
        or payload.get("to_number")
        or (payload.get("args") or {}).get("phone")
        or ""
    )
    e164 = to_e164(str(phone)) if phone else None
    dig = _digits(str(phone))
    if dig.endswith(dig[-10:]) if len(dig) >= 10 else dig:
        last10 = dig[-10:] if len(dig) >= 10 else dig
    else:
        last10 = dig

    email = str(
        payload.get("email")
        or payload.get("customer_email")
        or (payload.get("args") or {}).get("email")
        or ""
    ).strip().lower()

    call_id = str(
        payload.get("call_id")
        or payload.get("retell_call_id")
        or (payload.get("args") or {}).get("call_id")
        or ""
    ).strip()

    with crm.connect(db_path) as con:
        if call_id:
            row = con.execute(
                "SELECT * FROM leads WHERE retell_call_id = ? ORDER BY updated_at DESC LIMIT 1",
                (call_id,),
            ).fetchone()
            if row:
                return dict(row)

        if last10:
            rows = con.execute(
                "SELECT * FROM leads WHERE phone_primary LIKE ? OR phones_json LIKE ? "
                "ORDER BY updated_at DESC LIMIT 5",
                (f"%{last10}%", f"%{last10}%"),
            ).fetchall()
            for r in rows:
                d = dict(r)
                p = _digits(str(d.get("phone_primary") or ""))
                if p.endswith(last10) or last10 in _digits(str(d.get("phones_json") or "")):
                    return d

        if email:
            row = con.execute(
                "SELECT * FROM leads WHERE lower(COALESCE(email_primary,'')) = ? "
                "OR lower(COALESCE(emails_json,'')) LIKE ? ORDER BY updated_at DESC LIMIT 1",
                (email, f"%{email}%"),
            ).fetchone()
            if row:
                return dict(row)

    return None


def public_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Safe subset for the voice agent."""
    keys = [
        "id",
        "first_name",
        "last_name",
        "full_name",
        "phone_primary",
        "email_primary",
        "property_address",
        "property_city",
        "property_state",
        "property_zip",
        "beds",
        "baths",
        "sqft",
        "year_built",
        "garage_type",
        "lot_size_acres",
        "basement_type",
        "occupancy",
        "motivation",
        "timeline",
        "walk_away_ask",
        "house_info_summary",
        "condition_notes",
        "status",
        "qualified",
        "preferred_call_window",
        "best_time_to_call",
        "ai_call_consent",
        "website_opt_in",
        "appointment_at",
        "alex_notes",
        "lisa_notes",
        "retell_call_id",
    ]
    out = {k: lead.get(k) for k in keys if lead.get(k) not in (None, "")}
    out["lead_id"] = lead.get("id")
    return out


def lookup_lead(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    lead = find_lead(db_path, args if isinstance(args, dict) else payload)
    if not lead:
        return {"ok": False, "found": False, "error": "lead_not_found"}
    return {"ok": True, "found": True, "lead": public_lead(lead)}


def crm_upsert_lead(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    if not isinstance(args, dict):
        args = {}
    # Flatten nested lead object
    if isinstance(args.get("lead"), dict):
        merged = {**args.get("lead"), **{k: v for k, v in args.items() if k != "lead"}}
        args = merged

    lead_id = str(args.get("lead_id") or args.get("id") or "").strip()
    if not lead_id:
        # try resolve existing
        existing = find_lead(db_path, args)
        if existing:
            lead_id = str(existing["id"])
        else:
            # create id from phone or temp
            phone = to_e164(str(args.get("phone") or args.get("phone_primary") or "")) or "unknown"
            lead_id = f"lead-{phone.replace('+','')}"

    fields: dict[str, Any] = {}
    mapping = {
        "first_name": "first_name",
        "last_name": "last_name",
        "full_name": "full_name",
        "name": "full_name",
        "phone": "phone_primary",
        "phone_primary": "phone_primary",
        "email": "email_primary",
        "email_primary": "email_primary",
        "property_address": "property_address",
        "address": "property_address",
        "property_city": "property_city",
        "city": "property_city",
        "property_state": "property_state",
        "state": "property_state",
        "property_zip": "property_zip",
        "zip": "property_zip",
        "beds": "beds",
        "baths": "baths",
        "sqft": "sqft",
        "year_built": "year_built",
        "garage_type": "garage_type",
        "lot_size_acres": "lot_size_acres",
        "basement_type": "basement_type",
        "occupancy": "occupancy",
        "motivation": "motivation",
        "timeline": "timeline",
        "walk_away_ask": "walk_away_ask",
        "house_info_summary": "house_info_summary",
        "condition_notes": "condition_notes",
        "preferred_call_window": "preferred_call_window",
        "best_time_to_call": "best_time_to_call",
        "alex_notes": "alex_notes",
        "qualified": "qualified",
        "status": "status",
        "appointment_at": "appointment_at",
        "ai_call_consent": "ai_call_consent",
        "website_opt_in": "website_opt_in",
    }
    for src, dst in mapping.items():
        if src in args and args[src] is not None and args[src] != "":
            fields[dst] = args[src]

    if "phone_primary" in fields:
        e = to_e164(str(fields["phone_primary"]))
        if e:
            fields["phone_primary"] = e
            fields["phones_json"] = json.dumps([e])
    if "email_primary" in fields:
        fields["emails_json"] = json.dumps([str(fields["email_primary"])])

    if "full_name" in fields and "first_name" not in fields:
        parts = str(fields["full_name"]).split()
        if parts:
            fields.setdefault("first_name", parts[0])
            if len(parts) > 1:
                fields.setdefault("last_name", " ".join(parts[1:]))

    fields.setdefault("owner_agent", "alex")
    crm.ensure_retell_columns(db_path)
    lead = crm.upsert_lead_fields(db_path, lead_id, fields, actor="alex")
    crm.add_activity(
        db_path,
        lead_id,
        "crm_upsert_lead",
        f"Alex upsert keys={list(fields.keys())}",
        actor="alex",
        payload={"fields": list(fields.keys())},
    )
    return {"ok": True, "lead_id": lead_id, "lead": public_lead(lead)}


def crm_log_activity(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    if not isinstance(args, dict):
        args = {}
    lead = find_lead(db_path, args)
    lead_id = str(args.get("lead_id") or (lead or {}).get("id") or "").strip()
    if not lead_id:
        return {"ok": False, "error": "lead_id_required"}
    kind = str(args.get("kind") or args.get("activity_type") or "alex_note")
    summary = str(args.get("summary") or args.get("note") or args.get("message") or "").strip()
    if not summary:
        return {"ok": False, "error": "summary_required"}
    extra = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    crm.add_activity(db_path, lead_id, kind, summary[:4000], actor="alex", payload=extra)
    # optionally append alex_notes
    if args.get("also_alex_notes"):
        existing = crm.get_lead(db_path, lead_id) or {}
        prev = str(existing.get("alex_notes") or "")
        note = (prev + "\n" + summary).strip() if prev else summary
        crm.upsert_lead_fields(db_path, lead_id, {"alex_notes": note[:8000]}, actor="alex")
    return {"ok": True, "lead_id": lead_id, "logged": True, "kind": kind}


def calendar_book_ryan(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Book Ryan appointment window on the lead (CRM). Full calendar integration later."""
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    if not isinstance(args, dict):
        args = {}
    lead = find_lead(db_path, args)
    lead_id = str(args.get("lead_id") or (lead or {}).get("id") or "").strip()
    if not lead_id:
        return {"ok": False, "error": "lead_id_required"}

    when = str(
        args.get("appointment_at")
        or args.get("start")
        or args.get("datetime")
        or args.get("slot")
        or ""
    ).strip()
    timezone = str(args.get("timezone") or "America/New_York")
    notes = str(args.get("notes") or args.get("summary") or "").strip()

    if not when:
        return {"ok": False, "error": "appointment_at_required"}

    fields = {
        "appointment_at": when,
        "status": "ALEX_MANAGING",
        "owner_agent": "ryan",
        "preferred_call_window": when,
    }
    crm.upsert_lead_fields(db_path, lead_id, fields, actor="alex")
    crm.add_activity(
        db_path,
        lead_id,
        "calendar_book_ryan",
        f"Ryan appointment requested: {when} ({timezone}) {notes}".strip(),
        actor="alex",
        payload={"appointment_at": when, "timezone": timezone, "notes": notes},
    )
    # appointments table optional; activity + lead.appointment_at is source of truth

    return {
        "ok": True,
        "lead_id": lead_id,
        "appointment_at": when,
        "timezone": timezone,
        "status": "ALEX_MANAGING",
        "message": "Ryan appointment saved on lead; team will confirm.",
    }


def suppress_lead(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    if not isinstance(args, dict):
        args = {}
    lead = find_lead(db_path, args)
    lead_id = str(args.get("lead_id") or (lead or {}).get("id") or "").strip()
    if not lead_id:
        return {"ok": False, "error": "lead_id_required"}
    reason = str(args.get("reason") or args.get("stop_reason") or "caller_requested_stop").strip()
    crm.mark_call(
        db_path,
        lead_id,
        status="SUPPRESSED",
        extra={"dnc_flag": "Y", "stop_reason": reason, "owner_agent": "alex"},
    )
    crm.add_activity(
        db_path,
        lead_id,
        "suppress_lead",
        reason,
        actor="alex",
        payload={"reason": reason},
    )
    return {"ok": True, "lead_id": lead_id, "status": "SUPPRESSED", "dnc": True, "reason": reason}
