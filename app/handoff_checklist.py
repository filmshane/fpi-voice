"""Lisa → Alex handoff checklist (CRM gate before Retell)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.retell_dispatch import lead_phones, to_e164

# Statuses that may proceed to Alex after YES / website opt-in
READY_STATUSES = {
    "APPROVED_LEAD_SENDING_ALEX",
    "NEW_LISA_LEAD",
    "awaiting_ai_consent",  # legacy
    "handed_to_alex",  # legacy
    "APPROVED_LEAD_SENDING_ALEX".lower(),
}

BLOCKED_STATUSES = {
    "SUPPRESSED",
    "DISQUALIFIED",
    "DEAD",
    "CURR_ALEX",  # already with Alex / in call path
    "SCOUTING_LEAD",
    "WAITING_MAX_PRICE_SHANE",
    "CONTRACT_SIGNED",
    "CLOSED",
}

DEFAULT_REDIAL_COOLDOWN_MINUTES = 45


def _truthy(v: Any) -> bool:
    if v is True or v == 1:
        return True
    if v is None or v is False or v == 0:
        return False
    s = str(v).strip().lower()
    return s in ("1", "y", "yes", "true", "t", "on")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def evaluate_handoff_checklist(
    lead: dict[str, Any],
    *,
    redial_cooldown_minutes: int = DEFAULT_REDIAL_COOLDOWN_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Lisa complete-checklist gate before Alex/Retell.

    Required:
      - ai_call_consent OR website_opt_in
      - phone present and valid E.164 (+1 US 10-digit)
      - not DNC / not SUPPRESSED
      - status in APPROVED_LEAD_SENDING_ALEX or NEW_LISA_LEAD (after YES)
      - no open retell_call_id in last N minutes
    """
    now = now or datetime.now(timezone.utc)
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    status = str(lead.get("status") or "").strip()
    status_u = status.upper()

    # --- consent ---
    consent = _truthy(lead.get("ai_call_consent")) or _truthy(lead.get("website_opt_in"))
    checks.append(
        {
            "id": "consent",
            "ok": consent,
            "detail": "ai_call_consent or website_opt_in must be true",
            "ai_call_consent": lead.get("ai_call_consent"),
            "website_opt_in": lead.get("website_opt_in"),
        }
    )
    if not consent:
        blockers.append("missing_ai_call_consent_or_website_opt_in")

    # --- phone E.164 ---
    raw_phone = lead_phones(lead)
    e164 = to_e164(raw_phone)
    phone_ok = bool(e164)
    checks.append(
        {
            "id": "phone_e164",
            "ok": phone_ok,
            "detail": "phone_primary (or phones_json[0]) must normalize to +1XXXXXXXXXX",
            "raw": raw_phone or None,
            "e164": e164,
        }
    )
    if not phone_ok:
        blockers.append("invalid_or_missing_phone")

    # --- DNC / suppressed ---
    dnc = _truthy(lead.get("dnc_flag")) or str(lead.get("stop_reason") or "").strip() != ""
    suppressed = status_u == "SUPPRESSED" or _truthy(lead.get("suppressed"))
    dnc_ok = not dnc and not suppressed
    checks.append(
        {
            "id": "not_dnc_or_suppressed",
            "ok": dnc_ok,
            "detail": "dnc_flag/stop_reason/SUPPRESSED must be clear",
            "dnc_flag": lead.get("dnc_flag"),
            "status": status,
        }
    )
    if not dnc_ok:
        blockers.append("dnc_or_suppressed")

    # --- status path ---
    # Allow NEW_LISA_LEAD only if consent already true (YES just happened)
    # Prefer APPROVED_LEAD_SENDING_ALEX
    # Preferred: APPROVED_LEAD_SENDING_ALEX. NEW_LISA_LEAD OK after YES (consent checked separately).
    # CURR_ALEX / ALEX_MANAGING allowed only if redial cooldown passes (checked below).
    allowed = {
        "APPROVED_LEAD_SENDING_ALEX",
        "NEW_LISA_LEAD",
        "CURR_ALEX",
        "ALEX_MANAGING",
        "AWAITING_AI_CONSENT",
        "HANDED_TO_ALEX",
        "LISA_TEXTING",
    }
    terminal = {"SUPPRESSED", "DISQUALIFIED", "DEAD", "CLOSED", "CONTRACT_SIGNED", "ASSIGNED_TO_FLIPPER"}
    status_ok = status_u in allowed or status in (
        "awaiting_ai_consent",
        "handed_to_alex",
        "lisa_texting",
    )
    if status_u in terminal:
        status_ok = False
    checks.append(
        {
            "id": "status_ready",
            "ok": status_ok,
            "detail": "status should be APPROVED_LEAD_SENDING_ALEX (or NEW_LISA_LEAD after YES)",
            "status": status,
        }
    )
    if not status_ok:
        blockers.append(f"status_not_ready:{status or 'empty'}")

    # --- redial cooldown ---
    call_id = (lead.get("retell_call_id") or "").strip()
    last_evt = str(lead.get("last_retell_event") or "")
    updated = _parse_ts(str(lead.get("updated_at") or "")) or _parse_ts(
        str(lead.get("qualified_at") or "")
    )
    recent_call = False
    if call_id and updated:
        age = now - updated.astimezone(timezone.utc)
        # Only block if last event looks like an active/recent dial
        if age <= timedelta(minutes=redial_cooldown_minutes):
            if last_evt in (
                "dispatch_outbound",
                "call_started",
                "call_ended",
                "",
            ) or "dispatch" in last_evt or "start" in last_evt.lower():
                recent_call = True
    elif call_id and not updated:
        # unknown time but has call id — be conservative if last event is dispatch
        if "dispatch" in last_evt or last_evt in ("call_started", ""):
            recent_call = True

    redial_ok = not recent_call
    checks.append(
        {
            "id": "no_recent_retell_call",
            "ok": redial_ok,
            "detail": f"no retell_call_id activity in last {redial_cooldown_minutes} minutes",
            "retell_call_id": call_id or None,
            "last_retell_event": last_evt or None,
            "updated_at": lead.get("updated_at"),
        }
    )
    if not redial_ok:
        blockers.append("recent_retell_call_cooldown")

    # --- Lisa data completeness (soft required for handoff quality) ---
    name_ok = bool(
        (lead.get("first_name") or lead.get("last_name") or lead.get("full_name") or "").strip()
    )
    addr_ok = bool((lead.get("property_address") or "").strip())
    checks.append({"id": "name_present", "ok": name_ok, "detail": "first/last or full_name"})
    checks.append(
        {"id": "address_present", "ok": addr_ok, "detail": "property_address preferred before Alex"}
    )
    # Name/address are required for *complete* Lisa checklist (user asked complete checklist)
    if not name_ok:
        blockers.append("missing_name")
    if not addr_ok:
        blockers.append("missing_property_address")

    ready = len(blockers) == 0
    return {
        "ready": ready,
        "blockers": blockers,
        "checks": checks,
        "e164": e164,
        "recommended_status_before_dial": "APPROVED_LEAD_SENDING_ALEX",
        "status_after_successful_dial": "CURR_ALEX",
        "redial_cooldown_minutes": redial_cooldown_minutes,
    }


def lisa_complete_payload(
    lead_id: str,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    property_address: str | None = None,
    ai_call_consent: bool = True,
    website_opt_in: bool | None = None,
    preferred_call_window: str | None = None,
    consent_text: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Fields Lisa should write to CRM when checklist is complete (pre-dispatch)."""
    e164 = to_e164(phone) if phone else None
    full = " ".join(x for x in [(first_name or "").strip(), (last_name or "").strip()] if x)
    body: dict[str, Any] = {
        "id": lead_id,
        "status": "APPROVED_LEAD_SENDING_ALEX",
        "owner_agent": "lisa",
        "ai_call_consent": 1 if ai_call_consent else 0,
        "consent_text": consent_text
        or "Yes, an AI from First Property Investment may call me about selling my property.",
    }
    if website_opt_in is not None:
        body["website_opt_in"] = 1 if website_opt_in else 0
    if first_name:
        body["first_name"] = first_name.strip()
    if last_name:
        body["last_name"] = last_name.strip()
    if full:
        body["full_name"] = full
    if e164:
        body["phone_primary"] = e164
        body["phones_json"] = json.dumps([e164])
    if property_address:
        body["property_address"] = property_address.strip()
    if preferred_call_window:
        body["preferred_call_window"] = preferred_call_window
        body["best_time_to_call"] = preferred_call_window
    if notes:
        body["lisa_notes"] = notes
    return body
