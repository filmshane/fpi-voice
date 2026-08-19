# ElevenLabs post-call webhook (FPI Alex)

## Public URL (set this in ElevenLabs Agents → Webhooks)

```
https://firstpropertyinvestment.us/api/voice/alex-elevenlabs-webhook
```

Aliases (same handler):
- `https://firstpropertyinvestment.us/api/voice/elevenlabs/post-call`
- `https://firstpropertyinvestment.us/api/voice/webhooks/elevenlabs/post-call`

## Events handled
| type | Action |
|------|--------|
| `post_call_transcription` | CRM notes + status (`ALEX_MANAGING` / `SCOUTING_LEAD` / `DISQUALIFIED`) |
| `post_call_audio` | Save MP3 under `/opt/fpi-voice/data/elevenlabs-webhooks/audio/` |
| `call_initiation_failure` | Status `ALEX_CALL_FAILED` + reason |

## Security
1. Copy webhook **HMAC secret** from ElevenLabs Agents settings into:
   ```
   /opt/fpi-voice/.env → ELEVENLABS_WEBHOOK_SECRET=...
   ```
2. Restart: `sudo systemctl restart fpi-voice-api`
3. Server verifies header `elevenlabs-signature` / `ElevenLabs-Signature`:
   - format `t=<unix>,v0=<hex>`
   - HMAC-SHA256 of `{timestamp}.{raw_body}` with the secret
4. Always returns **HTTP 200** on successful accept (ElevenLabs auto-disables after 10 consecutive failures).

## Lead matching
Looks for `lead_id` in:
`data.conversation_initiation_client_data.dynamic_variables.lead_id`
then phone, then prior `elevenlabs_conversation_id`.

Pass `lead_id` in outbound dynamic variables when starting the call.

## Retell webhook
**Scrapped.** Old paths return **410**:
- `/api/voice/alex-retell-webhook`
- `/api/voice/retell-webhook`

## Local test
```bash
SECRET='your_secret'
BODY='{"type":"post_call_transcription","event_timestamp":1739537297,"data":{"agent_id":"x","conversation_id":"c1","status":"done","analysis":{"transcript_summary":"test","call_successful":"success"},"conversation_initiation_client_data":{"dynamic_variables":{"lead_id":"lead-1513-18th-st-nw-cleveland","customer.name":"Shane Miller"}}}}'
TS=$(date +%s)
SIG=$(python3 - <<PY
import hmac,hashlib,os
secret=os.environ['SECRET']
ts=os.environ['TS']
body=os.environ['BODY']
print('v0='+hmac.new(secret.encode(), f'{ts}.{body}'.encode(), hashlib.sha256).hexdigest())
PY
)
curl -sS -X POST http://127.0.0.1:8792/api/voice/alex-elevenlabs-webhook \
  -H "Content-Type: application/json" \
  -H "elevenlabs-signature: t=${TS},${SIG}" \
  -d "$BODY"
```
