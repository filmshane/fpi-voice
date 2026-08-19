# Retell Alex — custom tools + knowledge base

Agent: `agent_deaec073f1969cc0341dbfa620`  
Base (public): `https://firstpropertyinvestment.us`  
Local: `http://127.0.0.1:8792`

## Post-call webhook
```
POST https://firstpropertyinvestment.us/api/voice/alex-retell-webhook
```
Events: call_started, call_ended, call_analyzed

## Custom tool webhook URLs (use these in Retell)

All methods: **POST** · Content-Type: **application/json**  
Body may be flat or `{ "args": { ... } }` (both accepted).

| Tool name | URL |
|-----------|-----|
| **lookup_lead** | `https://firstpropertyinvestment.us/api/voice/tools/lookup_lead` |
| **crm_upsert_lead** | `https://firstpropertyinvestment.us/api/voice/tools/crm_upsert_lead` |
| **crm_log_activity** | `https://firstpropertyinvestment.us/api/voice/tools/crm_log_activity` |
| **calendar_book_ryan** | `https://firstpropertyinvestment.us/api/voice/tools/calendar_book_ryan` |
| **suppress_lead** | `https://firstpropertyinvestment.us/api/voice/tools/suppress_lead` |

### Parameter schemas (for Retell tool config)

#### lookup_lead
```json
{
  "lead_id": "string (optional)",
  "phone": "string (optional)",
  "email": "string (optional)",
  "call_id": "string (optional)"
}
```
Returns: `{ ok, found, lead: { lead_id, first_name, property_address, ... } }`

#### crm_upsert_lead
```json
{
  "lead_id": "string",
  "first_name": "string",
  "last_name": "string",
  "phone": "string",
  "email": "string",
  "property_address": "string",
  "beds": "number",
  "baths": "number",
  "sqft": "number",
  "year_built": "number",
  "garage_type": "string",
  "motivation": "string",
  "timeline": "string",
  "walk_away_ask": "string",
  "house_info_summary": "string",
  "qualified": "Y|N",
  "alex_notes": "string"
}
```

#### crm_log_activity
```json
{
  "lead_id": "string",
  "summary": "string (required)",
  "kind": "string (optional, default alex_note)",
  "also_alex_notes": true
}
```

#### calendar_book_ryan
```json
{
  "lead_id": "string",
  "appointment_at": "string (required, e.g. 2026-08-15T10:00:00-04:00)",
  "timezone": "America/New_York",
  "notes": "string"
}
```

#### suppress_lead
```json
{
  "lead_id": "string",
  "reason": "string (e.g. caller said stop)"
}
```

Always pass `lead_id` from dynamic variable `{{lead_id}}` when available.

## Knowledge base documents (upload to Retell KB / FAQ node)

Folder: `/home/shanem/FPI-Corp/Alex/retell-kb/`

| File | Purpose |
|------|---------|
| `00-company-and-compliance.md` | Company, AI disclosure, TCPA |
| `01-seller-faq-handbook.md` | Full polished seller FAQ handbook |
| `02-voice-agent-kb-snippet.md` | Short voice snippet |
| `03-seller-faq-qa-pairs.md` | 242 Q&A pairs for objections |

Canonical FAQ DB (not all needed in Retell UI): `/home/shanem/FPI-Corp/Alex/FAQ/`

### Retell setup
1. Agent → Knowledge Base → upload the four files above (or at least 00 + 01 + 03).
2. Agent → Functions / Tools → add five custom tools with the URLs and parameters.
3. Prompt: tell Alex to call `lookup_lead` at start if needed, `crm_upsert_lead` as facts are collected, `crm_log_activity` for key notes, `calendar_book_ryan` when booking, `suppress_lead` on STOP.
