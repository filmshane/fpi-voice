# FPI Retell voice plumbing (Alex)

**Agent ID:** `agent_deaec073f1969cc0341dbfa620`  
**Service:** `/opt/fpi-voice` · systemd `fpi-voice-api.service` · port `127.0.0.1:8792`  
**Public webhook (when DNS/TLS hits this host):**  
`https://firstpropertyinvestment.us/api/voice/alex-retell-webhook`

HTML under `/var/www/firstpropertyinvestment.us` is **never moved** — only nginx `location /api/voice/` proxy added.

## Architecture

```
Website / CRM consent (YES + phone)
  → POST /api/voice/dispatch/{lead_id}   (local or internal)
  → POST https://api.retellai.com/v2/create-phone-call
       override_agent_id = agent_deaec073f1969cc0341dbfa620
       retell_llm_dynamic_variables = {{first_name}}, {{address}}, …

Alex (Retell) talks
  → webhook call_started / call_ended / call_analyzed
  → /api/voice/alex-retell-webhook → FPI CRM statuses
```

Same pattern as SOL-RIGHT (`/opt/sol-right` + agent_3f938…).

## Dashboard checklist

1. Open https://dashboard.retellai.com/agents/agent_deaec073f1969cc0341dbfa620  
2. Prompt uses variables from dynamic vars table below  
3. Phone Numbers → bind **Outbound agent** = this agent (same from-number as SOL-RIGHT OK if shared account)  
4. Webhooks → `https://firstpropertyinvestment.us/api/voice/alex-retell-webhook`  
   Events: `call_started`, `call_ended`, `call_analyzed`  
5. Optional custom analysis: `qualified` (Y/N), `motivation`, `timeline`, `walk_away_ask`

## Dynamic variables (inject via API)

| Variable | Use |
|----------|-----|
| `{{first_name}}` `{{customer_name}}` | Greeting |
| `{{address}}` / `{{property_address}}` | Subject property |
| `{{phone}}` | Callback confirm |
| `{{beds}}` `{{baths}}` `{{sqft}}` `{{year_built}}` | House facts |
| `{{garage_type}}` `{{lot_size_acres}}` | |
| `{{motivation}}` `{{timeline}}` `{{walk_away_ask}}` | |
| `{{opening_script}}` | AI identity + opt-out open |
| `{{lead_id}}` | CRM writeback |
| `{{company_name}}` `{{company_website}}` | FPI branding |

## Env (`/opt/fpi-voice/.env`)

```
RETELL_API_KEY=…          # shared lab key from SOL-RIGHT
RETELL_AGENT_ID=agent_deaec073f1969cc0341dbfa620
RETELL_FROM_NUMBER=+1…
CRM_DB_PATH=/home/shanem/FPI-Corp/CRM/fpi_crm.db
PUBLIC_BASE_URL=https://firstpropertyinvestment.us
```

## Ops

```bash
sudo systemctl status fpi-voice-api
curl -s http://127.0.0.1:8792/api/health | python3 -m json.tool
# Dispatch demo lead (after consent flags set):
curl -s -X POST http://127.0.0.1:8792/api/voice/dispatch/lead-1513-18th-st-nw-cleveland | python3 -m json.tool
# Poll call if webhooks not public yet:
curl -s http://127.0.0.1:8792/api/voice/calls/CALL_ID | python3 -m json.tool
```

## LLM note

Retell hosts the **voice** agent runtime. FPI back-office / Scout still use Hermes  
`grok-4.20-reasoning` via `:8645`. Point Retell agent LLM to custom OpenAI endpoint  
only if you expose a public bridge to that proxy.

## Agent-required dynamic variables (all strings)

When creating a **web call** or **phone call**, always pass:

```json
{
  "agent_id": "agent_deaec073f1969cc0341dbfa620",
  "retell_llm_dynamic_variables": {
    "customer_name": "John Doe",
    "property_address": "123 Main St, Springfield, IL 62704",
    "crm_summary": "Inbound lead from website form, asked about cash offer",
    "lead_id": "lead_12345"
  }
}
```

| Variable | Source |
|----------|--------|
| `customer_name` | CRM full name |
| `property_address` | CRM property address |
| `crm_summary` | Auto-built CRM brief (status, consent, motivation, notes) |
| `lead_id` | CRM lead id |

### FPI endpoints
- Preview: `GET /api/voice/web-call/preview/{lead_id}`
- Create web call: `POST /api/voice/web-call/{lead_id}`
- Inline website: `POST /api/voice/web-call` with form fields
- Phone dispatch still: `POST /api/voice/dispatch/{lead_id}` (same dyn vars)

## Retell flow dyn vars (authoritative)
- `{{customer.name}}` → key `customer.name` (Greeting)
- `{{first_name}}` / `{{last_name}}` → Confirm Name + crm_upsert_lead
- `{{lead_id}}` → tools (lookup/upsert/calendar/suppress/log)
- Phone is **not** a prompt placeholder. Dial via top-level `to_number` on create-phone-call.
- CRM phone field is `phone_primary` written by tools, not inbound `{{phone}}`.
- Do not send redundant `customer_phone` / `phone` / `phone_number` / dyn `to_number` aliases.

