"""Retell outbound dispatch for FPI Alex."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("fpi.retell")

PHONE_OPENING = """\
Hi {first_name}, this is Alex, an AI representative with {company}.
I'm an automated assistant — not a human. You can say stop anytime and I'll end the call and remove you from outreach.
You opted in on our website{window_bit} about a possible cash offer on {address}.
Is now still a good time for a short call?
"""


def to_e164(phone: str | None) -> str | None:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+1{digits}"


def lead_phones(lead: dict[str, Any]) -> str:
    if lead.get("phone_primary"):
        return str(lead["phone_primary"])
    import json

    try:
        phones = json.loads(lead.get("phones_json") or "[]")
        if phones:
            return str(phones[0])
    except Exception:
        pass
    return ""


# Prompt/tool vars the Retell conversation flow actually consumes (all strings).
# {{customer.name}} → key "customer.name"
# {{first_name}} / {{last_name}} → Confirm Name + crm_upsert_lead
# {{lead_id}} → lookup_lead / crm_* / calendar / suppress tools
# Phone is NOT a prompt placeholder — only top-level to_number on create-phone-call.
AGENT_REQUIRED_DYN_VARS = (
    "customer.name",
    "first_name",
    "last_name",
    "lead_id",
)

# Optional context still useful if nodes reference them
AGENT_CONTEXT_DYN_VARS = (
    "property_address",
    "crm_summary",
    "customer_name",  # flat alias some nodes may still use
    "name",
)


def _s(val: Any, default: str = "") -> str:
    """Retell requires every dynamic variable value to be a string."""
    if val is None:
        return default
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip() if not isinstance(val, str) else val.strip()


def build_crm_summary(lead: dict[str, Any], *, source_hint: str = "") -> str:
    """Short CRM brief for {{crm_summary}} — one block Alex can read aloud context from."""
    parts: list[str] = []
    src = _s(lead.get("source") or source_hint or lead.get("lead_source"))
    if src:
        parts.append(f"Source: {src}")
    status = _s(lead.get("status"))
    if status:
        parts.append(f"Status: {status}")
    if lead.get("ai_call_consent") in (1, "1", True, "Y", "yes", "true"):
        parts.append("AI-call consent: YES")
    window = _s(lead.get("preferred_call_window") or lead.get("best_time_to_call"))
    if window:
        parts.append(f"Call window: {window}")
    motiv = _s(lead.get("motivation"))
    if motiv:
        parts.append(f"Motivation: {motiv}")
    timeline = _s(lead.get("timeline"))
    if timeline:
        parts.append(f"Timeline: {timeline}")
    ask = _s(lead.get("walk_away_ask"))
    if ask:
        parts.append(f"Walk-away ask: {ask}")
    house = _s(lead.get("house_info_summary") or lead.get("condition_notes"))
    if house:
        parts.append(f"House: {house}")
    lisa = _s(lead.get("lisa_notes"))
    if lisa:
        parts.append(f"Lisa notes: {lisa[:400]}")
    alex = _s(lead.get("alex_notes"))
    if alex:
        parts.append(f"Alex notes: {alex[:400]}")
    if not parts:
        parts.append("Inbound lead; limited CRM detail yet.")
    return "; ".join(parts)[:1500]


def build_dynamic_variables(
    settings: Settings,
    lead: dict[str, Any],
    *,
    source_hint: str = "",
    crm_summary_override: str | None = None,
) -> dict[str, str]:
    first = _s(lead.get("first_name"))
    last = _s(lead.get("last_name"))
    full = _s(lead.get("full_name") or f"{first} {last}".strip()) or "there"
    if not first:
        first = full.split()[0] if full else "there"
    address = _s(
        lead.get("property_address")
        or ", ".join(
            x
            for x in [
                _s(lead.get("property_city")),
                _s(lead.get("property_state")),
                _s(lead.get("property_zip")),
            ]
            if x
        )
    ) or "your property"
    window = _s(lead.get("preferred_call_window") or lead.get("best_time_to_call"))
    window_bit = f" for a call ({window})" if window else ""
    opening = PHONE_OPENING.format(
        first_name=first,
        company=settings.company_name,
        window_bit=window_bit,
        address=address,
    )
    phone = lead_phones(lead)
    e164 = to_e164(phone) or phone
    email = _s(lead.get("email_primary"))
    if not email:
        try:
            emails = json.loads(lead.get("emails_json") or "[]")
            if emails:
                email = _s(emails[0])
        except Exception:
            email = ""

    lead_id = _s(lead.get("id") or lead.get("lead_id"))
    crm_summary = _s(crm_summary_override) or build_crm_summary(
        lead, source_hint=source_hint
    )
    phone_e164 = _s(e164 or phone)

    # Dyn vars = what the Retell *prompt/flow* reads as {{...}}.
    # Do NOT stuff telephony phone aliases here — flow never uses {{phone}} /
    # {{customer_phone}} / {{to_number}}. Phone for dialing is top-level
    # create-phone-call to_number only. CRM phone is phone_primary via tools.
    dyn: dict[str, str] = {
        # --- flow-required ---
        "customer.name": full,  # Greeting: {{customer.name}}
        "first_name": first,
        "last_name": last,
        "lead_id": lead_id,
        # --- flat name aliases (harmless; some prompts still use these) ---
        "customer_name": full,
        "name": full,
        # --- property / CRM brief context ---
        "property_address": address,
        "address": address,
        "crm_summary": crm_summary,
        "property_city": _s(lead.get("property_city")),
        "property_state": _s(lead.get("property_state")),
        "property_zip": _s(lead.get("property_zip")),
        "beds": _s(lead.get("beds")),
        "baths": _s(lead.get("baths")),
        "sqft": _s(lead.get("sqft")),
        "year_built": _s(lead.get("year_built")),
        "garage_type": _s(lead.get("garage_type")),
        "lot_size_acres": _s(lead.get("lot_size_acres")),
        "motivation": _s(lead.get("motivation")),
        "timeline": _s(lead.get("timeline")),
        "walk_away_ask": _s(lead.get("walk_away_ask")),
        "house_info_summary": _s(lead.get("house_info_summary")),
        "preferred_call_window": window,
        "email": email,
        "company_name": _s(settings.company_name),
        "company_website": _s(settings.company_website),
        "service_area": _s(settings.service_area),
        "opening_script": opening,
        "callback_reason": "Cash offer follow-up after website AI-call consent",
        # Single CRM-shaped field only if a tool/node ever needs seed value.
        # Not used as a spoken {{phone}} placeholder in the current flow.
        "phone_primary": phone_e164,
    }
    # Final safety: every value is str; required keys always present
    out = {k: _s(v) for k, v in dyn.items()}
    for req in AGENT_REQUIRED_DYN_VARS:
        out.setdefault(req, "")
        if not isinstance(out[req], str):
            out[req] = _s(out[req])
    return out


def _call_metadata(settings: Settings, lead: dict[str, Any], *, source: str) -> dict[str, str]:
    dyn_preview = build_dynamic_variables(settings, lead)
    phone = _s(to_e164(lead_phones(lead)) or lead_phones(lead))
    return {
        "lead_id": dyn_preview["lead_id"],
        "company": _s(settings.company_name),
        "source": source,
        "agent_role": "alex",
        "customer_name": dyn_preview.get("customer_name") or dyn_preview.get("customer.name", ""),
        "customer.name": dyn_preview.get("customer.name", ""),
        "property_address": dyn_preview.get("property_address", ""),
        # metadata only (webhook correlation) — not prompt dyn aliases
        "customer_phone": phone,
        "phone_primary": phone,
    }


async def create_phone_call(
    settings: Settings,
    *,
    lead: dict[str, Any],
    to_number: str | None = None,
    source_hint: str = "fpi-voice-phone",
) -> dict[str, Any]:
    api_key = settings.retell_api_key.strip()
    agent_id = settings.retell_agent_id.strip()
    from_number = settings.retell_from_number.strip()
    to_e = to_number or to_e164(lead_phones(lead))
    from_e = to_e164(from_number) or from_number

    if not api_key:
        return {"ok": False, "error": "RETELL_API_KEY not set"}
    if not agent_id:
        return {"ok": False, "error": "RETELL_AGENT_ID not set"}
    if not from_e:
        return {"ok": False, "error": "RETELL_FROM_NUMBER not set"}
    if not to_e:
        return {"ok": False, "error": "customer phone not valid E.164"}

    dyn = build_dynamic_variables(settings, lead, source_hint=source_hint)
    body = {
        "from_number": from_e,
        "to_number": to_e,
        "override_agent_id": agent_id,
        "retell_llm_dynamic_variables": dyn,
        "metadata": _call_metadata(settings, lead, source=source_hint),
    }
    url = f"{settings.retell_api_base.rstrip('/')}/v2/create-phone-call"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(url, headers=headers, json=body)
        text = r.text[:4000]
        if r.status_code >= 400:
            log.error("retell create-phone-call failed %s %s", r.status_code, text[:500])
            return {
                "ok": False,
                "provider": "retell",
                "http_status": r.status_code,
                "error": text,
                "request": {
                    "from_number": from_e,
                    "to_number": to_e,
                    "agent_id": agent_id,
                    "retell_llm_dynamic_variables": {
                        k: dyn[k] for k in AGENT_REQUIRED_DYN_VARS
                    },
                },
            }
        data = r.json() if "application/json" in r.headers.get("content-type", "") else {"raw": text}
        call_id = data.get("call_id") or data.get("id")
        return {
            "ok": True,
            "provider": "retell",
            "mode": "retell_create_phone_call",
            "http_status": r.status_code,
            "voice_call_id": call_id,
            "call_id": call_id,
            "response": data,
            "lead_id": lead.get("id"),
            "to_number": to_e,
            "from_number": from_e,
            "agent_id": agent_id,
            "dynamic_variables": dyn,
            "required_dynamic_variables": {k: dyn[k] for k in AGENT_REQUIRED_DYN_VARS},
        }


async def create_web_call(
    settings: Settings,
    *,
    lead: dict[str, Any],
    source_hint: str = "website_web_call",
    crm_summary_override: str | None = None,
) -> dict[str, Any]:
    """Create a browser/web call via Retell Create Web Call API.

    Body shape Retell expects (values must be strings):
      {
        "agent_id": "agent_…",
        "retell_llm_dynamic_variables": {
          "customer_name": "…",
          "property_address": "…",
          "crm_summary": "…",
          "lead_id": "…"
        }
      }
    """
    api_key = settings.retell_api_key.strip()
    agent_id = settings.retell_agent_id.strip()
    if not api_key:
        return {"ok": False, "error": "RETELL_API_KEY not set"}
    if not agent_id:
        return {"ok": False, "error": "RETELL_AGENT_ID not set"}

    dyn = build_dynamic_variables(
        settings,
        lead,
        source_hint=source_hint,
        crm_summary_override=crm_summary_override,
    )
    body = {
        "agent_id": agent_id,
        "retell_llm_dynamic_variables": dyn,
        "metadata": _call_metadata(settings, lead, source=source_hint),
    }
    url = f"{settings.retell_api_base.rstrip('/')}/v2/create-web-call"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(url, headers=headers, json=body)
        text = r.text[:4000]
        if r.status_code >= 400:
            log.error("retell create-web-call failed %s %s", r.status_code, text[:500])
            return {
                "ok": False,
                "provider": "retell",
                "mode": "retell_create_web_call",
                "http_status": r.status_code,
                "error": text,
                "request": {
                    "agent_id": agent_id,
                    "retell_llm_dynamic_variables": {
                        k: dyn[k] for k in AGENT_REQUIRED_DYN_VARS
                    },
                },
            }
        data = r.json() if "application/json" in r.headers.get("content-type", "") else {"raw": text}
        call_id = data.get("call_id") or data.get("id")
        access_token = data.get("access_token")
        return {
            "ok": True,
            "provider": "retell",
            "mode": "retell_create_web_call",
            "http_status": r.status_code,
            "voice_call_id": call_id,
            "call_id": call_id,
            "access_token": access_token,
            "response": data,
            "lead_id": lead.get("id") or dyn.get("lead_id"),
            "agent_id": agent_id,
            "dynamic_variables": dyn,
            "required_dynamic_variables": {k: dyn[k] for k in AGENT_REQUIRED_DYN_VARS},
        }


def preview_create_web_call_body(settings: Settings, lead: dict[str, Any]) -> dict[str, Any]:
    """Exact JSON body website/API should send to Retell create-web-call (no secrets)."""
    dyn = build_dynamic_variables(settings, lead, source_hint="website_web_call")
    required = {k: dyn[k] for k in AGENT_REQUIRED_DYN_VARS}
    return {
        "agent_id": settings.retell_agent_id.strip(),
        # Minimal set the conversation flow actually reads
        "retell_llm_dynamic_variables": required,
        # Full context set (still all strings; no phone alias spam)
        "retell_llm_dynamic_variables_full": dyn,
        "metadata": _call_metadata(settings, lead, source="website_web_call"),
        "notes": {
            "greeting_uses": "{{customer.name}}",
            "confirm_name_uses": ["{{first_name}}", "{{last_name}}"],
            "tools_use": "{{lead_id}}",
            "phone_dialing": "top-level to_number on create-phone-call only (not a prompt var)",
            "crm_phone_field": "phone_primary via crm_upsert_lead tool — not inbound {{phone}}",
        },
    }


async def get_call(settings: Settings, call_id: str) -> dict[str, Any]:
    api_key = settings.retell_api_key.strip()
    url = f"{settings.retell_api_base.rstrip('/')}/v2/get-call/{call_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            return {"ok": False, "http_status": r.status_code, "error": r.text[:2000]}
        return {"ok": True, "call": r.json()}
