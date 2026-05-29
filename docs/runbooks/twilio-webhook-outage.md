# Twilio webhook outage

## Symptom

- VT-202 alert: `twilio_webhook_5xx_rate > threshold` (or hard-limit Telegram)
- `tenants` table shows no new `twilio_inbound_events` rows for ≥ 10 min
- Customer report: messages sent to the WhatsApp number but no automated reply

## Detection

- VT-202 alert via Telegram
- Ops Console live stream shows zero new `pipeline_runs` of `trigger_kind='twilio_inbound'`
- Logfire dashboard: error rate spike on `/api/webhook/twilio/inbound`

## Triage

1. Check Vercel function logs for `/api/webhook/twilio/inbound` 5xx → if present, check Supabase reachability from Vercel function
2. Check Twilio Console → Phone Numbers → the WhatsApp number → Webhook configuration: confirm URL matches current Vercel deploy URL
3. Confirm Twilio signature validation succeeds — `gh api repos/.../deployments` for recent failed sig checks
4. If Twilio delivery is OK but orchestrator is not picking up: check `twilio_inbound_events` table for unprocessed rows + `pipeline_runs` for stuck workflows

## Resolution

1. If webhook URL drift: update Twilio Console → save → manual test message
2. If signature mismatch: rotate `TWILIO_AUTH_TOKEN` in Vercel env + Railway env (Fazal authorization required); restart
3. If orchestrator stalled: tail Railway logs for DBOS errors; investigate; restart service if stuck
4. If region failover ongoing (Supabase or Vercel): see supabase-region-failover.md

## Postmortem

- Incident log entry in `docs/clau/entries/CL-N.md`
- VT-N row if a code fix is needed (e.g., new replay defense, sig edge case)
- Update `apps/team-orchestrator/canaries/vt81_twilio_webhook_hardening.py` if new attack shape discovered

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
- Notes: VT-81 ships full hardening substrate; this runbook coordinates with VT-81 — replace body with VT-81's content once that PR lands
