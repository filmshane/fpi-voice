# ElevenLabs open-call — what FPI sends

## Endpoint
`POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call`

## Request headers (FPI → ElevenLabs)

| Header name | Value | Secret? | Purpose |
|---|---|---|---|
| `xi-api-key` | value of `ELEVENLABS_API_KEY` | **YES** | Authenticate open-call API |
| `Content-Type` | `application/json` | no | JSON body |
| `Accept` | `application/json` | no | Expect JSON response |

## Secrets map

| Env var (`/opt/fpi-voice/.env`) | Direction | Used for |
|---|---|---|
| `ELEVENLABS_API_KEY` | **Outbound** → header `xi-api-key` | Open call auth |
| `ELEVENLABS_AGENT_ID` | Outbound body `agent_id` | Which Alex agent |
| `ELEVENLABS_AGENT_PHONE_NUMBER_ID` | Outbound body `agent_phone_number_id` | From-number (Twilio import id) |
| `ELEVENLABS_WEBHOOK_SECRET` | **Inbound only** | Verify `elevenlabs-signature` on post-call webhook — **not sent on open call** |

## Body fields (top-level)

| Field | Required | Source | 1513 example |
|---|---|---|---|
| `agent_id` | yes | env | `<SET ELEVENLABS_AGENT_ID>` |
| `agent_phone_number_id` | yes | env | `<SET ELEVENLABS_AGENT_PHONE_NUMBER_ID>` |
| `to_number` | yes | CRM phone_primary | `+14242790225` |
| `call_recording_enabled` | no | default false | `False` |
| `telephony_call_config.ringing_timeout_secs` | no | default 60 | `60` |
| `telephony_call_config.twilio_call_recording_enabled` | no | default false | `False` |

## conversation_initiation_client_data

| Field | Purpose | 1513 |
|---|---|---|
| `user_id` | CRM lead id | `lead-1513-18th-st-nw-cleveland` |
| `source_info.source` | tag | `twilio` |
| `conversation_config_override.agent.first_message` | personalized open | Hi Shane, this is Alex… |
| `dynamic_variables` | `{{var}}` map | see below |

## dynamic_variables (all strings)

| Key | Prompt / tool use | CRM source | 1513 value |
|---|---|---|---|
| `customer.name` | {{customer.name}} greeting | full_name | Shane Miller |
| `first_name` | Confirm Name + tools | first_name | Shane |
| `last_name` | Confirm Name + tools | last_name | Miller |
| `lead_id` | tools + post-call match | id | lead-1513-18th-st-nw-cleveland |
| `customer_name` | flat alias | full_name | Shane Miller |
| `name` | flat alias | full_name | Shane Miller |
| `property_address` | subject property | property_address | 1513 18th St NW, Cleveland, TN 37311 |
| `address` | alias | property_address | 1513 18th St NW, Cleveland, TN 37311 |
| `crm_summary` | CRM brief | built | Source: fpi-elevenlabs-outbound; Status: ALEX_CALL_FAILED; AI-call consent: YES; Call window: 2026-0… |
| `property_city` | facts | property_city | Cleveland |
| `property_state` | facts | property_state | TN |
| `property_zip` | facts | property_zip | 37311 |
| `beds` | facts | beds | 4 |
| `baths` | facts | baths | 3 |
| `sqft` | facts | sqft | 2688 |
| `year_built` | facts | year_built | 1965 |
| `garage_type` | facts | garage_type | attached |
| `lot_size_acres` | facts | lot_size_acres | 0.46 |
| `motivation` | qualify | motivation | test |
| `timeline` | qualify | timeline | 30-60 days |
| `walk_away_ask` | qualify | walk_away_ask | 250000 |
| `house_info_summary` | facts | house_info_summary | 1965 brick ranch 4/3 2688sf, unfinished basement, 2-car attached, 0.46ac corner lot |
| `preferred_call_window` | timing | preferred/best_time | 2026-08-20T10:00:00-04:00 |
| `email` | contact | email_primary | demo.seller@example.com |
| `company_name` | brand | settings | First Property Investment |
| `company_website` | brand | settings | http://firstpropertyinvestment.us/ |
| `service_area` | brand | settings | Chattanooga TN / Cleveland TN |
| `opening_script` | long open text | built | (see JSON) |
| `callback_reason` | context | fixed | Cash offer follow-up after website AI-call consent |
| `phone_primary` | CRM seed for tools — NOT dial target | phone_primary | +14242790225 |

## Phone rules
- **Dial** = top-level `to_number` only.
- `phone_primary` in dyn vars is CRM context for tools / post-call matching.
- No redundant `phone` / `customer_phone` / dyn `to_number` aliases.

## FPI endpoints
- Preview: `GET http://127.0.0.1:8792/api/voice/elevenlabs/open-call/preview/{lead_id}`
- Place call: `POST http://127.0.0.1:8792/api/voice/elevenlabs/open-call/{lead_id}`

## Full example
`/home/shanem/FPI-Corp/Alex/elevenlabs-open-call-shane-miller-1513.json`

## Still needed in .env before a real dial
```
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=agent_...
ELEVENLABS_AGENT_PHONE_NUMBER_ID=phnum_...
ELEVENLABS_WEBHOOK_SECRET=...   # inbound HMAC only
```

