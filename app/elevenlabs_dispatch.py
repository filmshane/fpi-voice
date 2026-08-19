"""ElevenLabs outbound open-call (Twilio) — build request body from CRM lead.

API (official OpenAPI):
  POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call
  Header: xi-api-key: <ELEVENLABS_API_KEY>
  Header: Content-Type: application/json

Required body:
  agent_id, agent_phone_number_id, to_number

Optional:
  conversation_initiation_client_data (dynamic_variables, first_message override, …)
  call_recording_enabled
  telephony_call_config
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.retell_dispatch import (
    AGENT_REQUIRED_DYN_VARS,
    build_crm_summary,
    build_dynamic_variables,
    lead_phones,
    to_e164,
    _s,
)

log = logging.getLogger("fpi.elevenlabs.dispatch")

ELEVENLABS_API_BASE = "https://api.elevenlabs.io"
OUTBOUND_PATH = "/v1/convai/twilio/outbound-call"


def build_open_call_dynamic_variables(
    settings: Settings,
    lead: dict[str, Any],
    *,
    source_hint: str = "fpi-elevenlabs-outbound",
) -> dict[str, str]:
    """Dynamic variables for Alex agent_3101kzw4yn2fehvtcdn131x9yj56.

    Agent prompt placeholders (from agent config):
      {{now}} {{customer_number}} {{customer_name}} {{property_address}}
      {{crm_summary}} {{lisa_notes}} {{ai_call_consent}} {{lead_id}}

    first_message template uses {name} — we also send name + customer_name.
    All values MUST be strings.
    """
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    base = build_dynamic_variables(settings, lead, source_hint=source_hint)
    phone = _s(to_e164(lead_phones(lead)) or lead_phones(lead) or base.get("phone_primary"))
    full = _s(base.get("customer_name") or base.get("customer.name") or lead.get("full_name") or "there")
    first = _s(base.get("first_name") or (full.split()[0] if full else "there"))
    last = _s(base.get("last_name"))
    lead_id = _s(base.get("lead_id") or lead.get("id"))
    addr = _s(base.get("property_address") or lead.get("property_address") or "your property")

    consent_raw = lead.get("ai_call_consent")
    if consent_raw in (1, "1", True, "Y", "yes", "true", "TRUE"):
        consent = "true"
    elif consent_raw in (0, "0", False, "N", "no", "false", "FALSE"):
        consent = "false"
    else:
        consent = _s(consent_raw) or "unknown"

    # Exact agent placeholders first
    out: dict[str, str] = {
        "now": now,
        "customer_number": phone,
        "customer_name": full,
        "name": full,  # first_message uses {name}
        "property_address": addr,
        "crm_summary": _s(base.get("crm_summary")) or build_crm_summary(lead, source_hint=source_hint),
        "lisa_notes": _s(lead.get("lisa_notes")),
        "ai_call_consent": consent,
        "lead_id": lead_id,
        # Helpful extras for tools / future prompt nodes
        "first_name": first,
        "last_name": last,
        "phone_primary": phone,
        "email": _s(base.get("email") or lead.get("email_primary")),
        "property_city": _s(base.get("property_city") or lead.get("property_city")),
        "property_state": _s(base.get("property_state") or lead.get("property_state")),
        "property_zip": _s(base.get("property_zip") or lead.get("property_zip")),
        "beds": _s(base.get("beds") or lead.get("beds")),
        "baths": _s(base.get("baths") or lead.get("baths")),
        "sqft": _s(base.get("sqft") or lead.get("sqft")),
        "year_built": _s(base.get("year_built") or lead.get("year_built")),
        "garage_type": _s(base.get("garage_type") or lead.get("garage_type")),
        "lot_size_acres": _s(base.get("lot_size_acres") or lead.get("lot_size_acres")),
        "motivation": _s(base.get("motivation") or lead.get("motivation")),
        "timeline": _s(base.get("timeline") or lead.get("timeline")),
        "walk_away_ask": _s(base.get("walk_away_ask") or lead.get("walk_away_ask")),
        "house_info_summary": _s(base.get("house_info_summary") or lead.get("house_info_summary")),
        "preferred_call_window": _s(
            base.get("preferred_call_window")
            or lead.get("preferred_call_window")
            or lead.get("best_time_to_call")
        ),
        "company_name": _s(settings.company_name),
        "company_website": _s(settings.company_website),
        "service_area": _s(settings.service_area),
        "alex_notes": _s(lead.get("alex_notes")),
    }
    return {k: _s(v) for k, v in out.items()}


def build_first_message(settings: Settings, lead: dict[str, Any], dyn: dict[str, str]) -> str:
    """Resolved greeting (override). Prefer first name for natural 'Is this Shane?'."""
    name = (
        dyn.get("first_name")
        or dyn.get("customer_name")
        or dyn.get("name")
        or dyn.get("customer.name")
        or "there"
    )
    # If first_name missing but full name present, use first token
    if name and " " in name and not dyn.get("first_name"):
        name = name.split()[0]
    return (
        f"Hi, this is Alex with First Property Investment. Is this {name}? "
        f"I'm calling about the property you were texting us about — "
        f"did I catch you at an okay time?"
    )


def build_outbound_call_request(
    settings: Settings,
    lead: dict[str, Any],
    *,
    source_hint: str = "fpi-elevenlabs-outbound",
    to_number: str | None = None,
    call_recording_enabled: bool | None = False,
    ringing_timeout_secs: int = 60,
) -> dict[str, Any]:
    """Full JSON body for POST /v1/convai/twilio/outbound-call (no secrets)."""
    agent_id = (settings.elevenlabs_agent_id or "").strip()
    phone_id = (settings.elevenlabs_agent_phone_number_id or "").strip()
    to_e = to_number or to_e164(lead_phones(lead)) or lead_phones(lead)
    to_e = to_e164(to_e) or to_e

    dyn = build_open_call_dynamic_variables(settings, lead, source_hint=source_hint)
    first_message = build_first_message(settings, lead, dyn)
    lead_id = dyn.get("lead_id") or _s(lead.get("id"))

    body: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_phone_number_id": phone_id,
        "to_number": _s(to_e),
        "conversation_initiation_client_data": {
            # End-user id for ElevenLabs analytics / your CRM correlation
            "user_id": lead_id,
            "dynamic_variables": dyn,
            # first_message override enabled on agent; send resolved greeting so
            # "Is this {{customer_name}}?" never speaks the raw placeholder.
            "conversation_config_override": {
                "agent": {
                    "first_message": first_message,
                }
            },
            "source_info": {
                "source": "twilio",
                "version": "fpi-voice-1.3",
            },
        },
        "call_recording_enabled": bool(call_recording_enabled)
        if call_recording_enabled is not None
        else False,
        "telephony_call_config": {
            "ringing_timeout_secs": int(ringing_timeout_secs or 60),
            "twilio_call_recording_enabled": bool(call_recording_enabled),
        },
    }
    return body


def build_request_headers(settings: Settings) -> dict[str, str]:
    """HTTP headers FPI sends TO ElevenLabs (open call)."""
    key = (settings.elevenlabs_api_key or "").strip()
    return {
        "xi-api-key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def preview_open_call(
    settings: Settings,
    lead: dict[str, Any],
    *,
    source_hint: str = "fpi-elevenlabs-outbound",
) -> dict[str, Any]:
    """Documented package: endpoint + headers (redacted) + body + field catalog."""
    body = build_outbound_call_request(settings, lead, source_hint=source_hint)
    headers = build_request_headers(settings)
    key_set = bool(headers.get("xi-api-key"))
    return {
        "method": "POST",
        "url": f"{ELEVENLABS_API_BASE}{OUTBOUND_PATH}",
        "headers": {
            "xi-api-key": "<ELEVENLABS_API_KEY>" if key_set else "<NOT SET — put key in /opt/fpi-voice/.env>",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        "headers_configured": {
            "xi-api-key": key_set,
        },
        "secrets": {
            "ELEVENLABS_API_KEY": {
                "where": "Request header xi-api-key",
                "direction": "FPI → ElevenLabs (open call auth)",
                "env": "/opt/fpi-voice/.env",
                "configured": key_set,
            },
            "ELEVENLABS_WEBHOOK_SECRET": {
                "where": "NOT sent on open call",
                "direction": "ElevenLabs → FPI post-call webhook HMAC (inbound)",
                "env": "/opt/fpi-voice/.env",
                "configured": bool(settings.elevenlabs_webhook_secret),
                "note": "Used only to verify elevenlabs-signature on webhooks we receive",
            },
        },
        "body": body,
        "required_body_fields": ["agent_id", "agent_phone_number_id", "to_number"],
        "missing_required": [
            k
            for k, v in {
                "agent_id": body.get("agent_id"),
                "agent_phone_number_id": body.get("agent_phone_number_id"),
                "to_number": body.get("to_number"),
            }.items()
            if not v
        ],
        "field_catalog": FIELD_CATALOG,
    }


FIELD_CATALOG: dict[str, dict[str, str]] = {
    # --- top-level body ---
    "agent_id": {
        "location": "body",
        "required": "yes",
        "type": "string",
        "source": "ELEVENLABS_AGENT_ID env / ElevenLabs Agents dashboard",
        "purpose": "Which Alex agent answers the call",
        "example": "agent_xxxxxxxx",
    },
    "agent_phone_number_id": {
        "location": "body",
        "required": "yes",
        "type": "string",
        "source": "ELEVENLABS_AGENT_PHONE_NUMBER_ID env (Twilio number imported in 11labs)",
        "purpose": "Caller ID / from-number resource on ElevenLabs",
        "example": "phnum_xxxxxxxx",
    },
    "to_number": {
        "location": "body",
        "required": "yes",
        "type": "string E.164",
        "source": "CRM leads.phone_primary",
        "purpose": "Seller phone to dial",
        "example": "+14242790225",
    },
    "call_recording_enabled": {
        "location": "body",
        "required": "no",
        "type": "boolean",
        "source": "FPI default false",
        "purpose": "Ask Twilio/11labs path to record",
        "example": "false",
    },
    "telephony_call_config.ringing_timeout_secs": {
        "location": "body",
        "required": "no",
        "type": "int 1-999",
        "source": "FPI default 60",
        "purpose": "How long to ring before give-up",
        "example": "60",
    },
    "telephony_call_config.twilio_call_recording_enabled": {
        "location": "body",
        "required": "no",
        "type": "boolean",
        "source": "FPI default false",
        "purpose": "Twilio-side recording flag",
        "example": "false",
    },
    # --- conversation_initiation_client_data ---
    "conversation_initiation_client_data.user_id": {
        "location": "body.conversation_initiation_client_data",
        "required": "no",
        "type": "string",
        "source": "CRM lead id",
        "purpose": "ElevenLabs end-user id; helps match analytics to CRM",
        "example": "lead-1513-18th-st-nw-cleveland",
    },
    "conversation_initiation_client_data.dynamic_variables.*": {
        "location": "body.conversation_initiation_client_data.dynamic_variables",
        "required": "no (but we always send flow keys)",
        "type": "object of strings",
        "source": "CRM lead row",
        "purpose": "Prompt placeholders {{var}} inside the agent",
        "example": "see dynamic_variables table",
    },
    "conversation_initiation_client_data.conversation_config_override.agent.first_message": {
        "location": "body.conversation_initiation_client_data.conversation_config_override",
        "required": "no",
        "type": "string",
        "source": "Built from first_name + property_address + company",
        "purpose": "Personalized opening line for this lead",
        "example": "Hi Shane, this is Alex...",
    },
    "conversation_initiation_client_data.source_info.source": {
        "location": "body.conversation_initiation_client_data.source_info",
        "required": "no",
        "type": "enum string",
        "source": "fixed twilio",
        "purpose": "Initiation source tag",
        "example": "twilio",
    },
    # --- headers ---
    "xi-api-key": {
        "location": "header",
        "required": "yes",
        "type": "string secret",
        "source": "ELEVENLABS_API_KEY",
        "purpose": "Authenticate open-call API to ElevenLabs",
        "example": "<from elevenlabs.io profile>",
    },
    "Content-Type": {
        "location": "header",
        "required": "yes",
        "type": "string",
        "source": "fixed",
        "purpose": "JSON body",
        "example": "application/json",
    },
    "Accept": {
        "location": "header",
        "required": "no",
        "type": "string",
        "source": "fixed",
        "purpose": "Expect JSON response",
        "example": "application/json",
    },
    # --- dynamic variables (detailed) ---
    "dynamic_variables.customer.name": {
        "location": "dynamic_variables",
        "required": "yes (FPI)",
        "type": "string",
        "source": "CRM full_name / first+last",
        "purpose": "Greeting {{customer.name}}",
        "example": "Shane Miller",
    },
    "dynamic_variables.first_name": {
        "location": "dynamic_variables",
        "required": "yes (FPI)",
        "type": "string",
        "source": "CRM first_name",
        "purpose": "Confirm Name node + crm_upsert_lead",
        "example": "Shane",
    },
    "dynamic_variables.last_name": {
        "location": "dynamic_variables",
        "required": "yes (FPI)",
        "type": "string",
        "source": "CRM last_name",
        "purpose": "Confirm Name node + crm_upsert_lead",
        "example": "Miller",
    },
    "dynamic_variables.lead_id": {
        "location": "dynamic_variables",
        "required": "yes (FPI)",
        "type": "string",
        "source": "CRM leads.id",
        "purpose": "Tool webhooks + post-call CRM match",
        "example": "lead-1513-18th-st-nw-cleveland",
    },
    "dynamic_variables.property_address": {
        "location": "dynamic_variables",
        "required": "recommended",
        "type": "string",
        "source": "CRM property_address",
        "purpose": "Subject property in conversation",
        "example": "1513 18th St NW, Cleveland, TN 37311",
    },
    "dynamic_variables.crm_summary": {
        "location": "dynamic_variables",
        "required": "recommended",
        "type": "string",
        "source": "Built from status/consent/motivation/notes",
        "purpose": "One-block CRM brief for agent context",
        "example": "Status: …; AI-call consent: YES; …",
    },
    "dynamic_variables.phone_primary": {
        "location": "dynamic_variables",
        "required": "recommended",
        "type": "string E.164",
        "source": "CRM phone_primary",
        "purpose": "CRM field seed for tools (not used as dial target — dial is to_number)",
        "example": "+14242790225",
    },
}


async def create_outbound_call(
    settings: Settings,
    *,
    lead: dict[str, Any],
    source_hint: str = "fpi-elevenlabs-outbound",
) -> dict[str, Any]:
    """POST open-call to ElevenLabs. Returns ok/error package."""
    api_key = (settings.elevenlabs_api_key or "").strip()
    if not api_key:
        return {"ok": False, "error": "ELEVENLABS_API_KEY not set"}
    body = build_outbound_call_request(settings, lead, source_hint=source_hint)
    missing = [k for k in ("agent_id", "agent_phone_number_id", "to_number") if not body.get(k)]
    if missing:
        return {
            "ok": False,
            "error": f"missing required fields: {', '.join(missing)}",
            "body_preview": body,
            "hint": "Set ELEVENLABS_AGENT_ID and ELEVENLABS_AGENT_PHONE_NUMBER_ID in .env; CRM needs phone_primary",
        }

    url = f"{ELEVENLABS_API_BASE}{OUTBOUND_PATH}"
    headers = build_request_headers(settings)
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(url, headers=headers, json=body)
        text = r.text[:4000]
        if r.status_code >= 400:
            log.error("elevenlabs outbound-call failed %s %s", r.status_code, text[:500])
            return {
                "ok": False,
                "provider": "elevenlabs",
                "http_status": r.status_code,
                "error": text,
                "request_body": {**body, "conversation_initiation_client_data": {
                    **body["conversation_initiation_client_data"],
                    # keep dyn for debug
                }},
            }
        data = r.json() if "application/json" in r.headers.get("content-type", "") else {"raw": text}
        return {
            "ok": True,
            "provider": "elevenlabs",
            "mode": "twilio_outbound_call",
            "http_status": r.status_code,
            "response": data,
            "conversation_id": data.get("conversation_id") or data.get("callSid") or data.get("call_sid"),
            "lead_id": lead.get("id"),
            "to_number": body.get("to_number"),
            "agent_id": body.get("agent_id"),
            "request_body": body,
        }
