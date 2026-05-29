# Twilio inbound webhook runbook (VT-81)

Detailed counterpart to `docs/runbooks/twilio-webhook-outage.md`. Use this for hardening-related incidents (replay attacks, signature drift, rate-limit triggers) rather than substrate-outage triage.

## Endpoints

- `POST /api/team/twilio/webhook` (apps/team-web) — Twilio inbound webhook

## Hardening surfaces (VT-81)

| Surface | Mechanism | Audit |
|---|---|---|
| Rate limit | 30 req/min/source-IP, in-memory sliding window | `console.warn` event `twilio_rate_limited`; 429 to Twilio |
| Replay defense | `twilio_inbound_replay` PRIMARY KEY (message_sid); INSERT ON CONFLICT DO NOTHING | `console.warn` event `twilio_replay_rejected`; 200 to Twilio (no re-process) |
| Signature verify | `verifyTwilioSignature` — constant-time | `console.warn` event `twilio_sig_invalid` with `sig_fingerprint` (first-8 sha256 chars); 403 to Twilio |
| PII redact | `redactForLog` (lib/log-redact.ts) applied to every console line | E.164 phones, Twilio SIDs, bare digit runs ≥ 7, emails all redacted |

## Signature rotation procedure

1. Generate new auth token in Twilio Console → Phone Numbers → the number → API credentials
2. Update Vercel env `TEAM_TWILIO_AUTH_TOKEN` (Fazal authorization required)
3. Update Railway env if orchestrator-side validation is wired
4. Restart Vercel deployment (manual redeploy)
5. Verify next inbound webhook lands successfully

## Replay-table maintenance

`twilio_inbound_replay` is append-only. Rows older than 5 min are no longer load-bearing for replay defense, but kept for ad-hoc audit. File a separate VT row to schedule TTL cleanup (e.g., daily DELETE WHERE received_at < now() - interval '7 days').

## Rate-limit override path

If a legitimate burst surfaces (e.g., a mass-message marketing run from a single Twilio number):

1. Identify the source IP from `console.warn` `twilio_rate_limited` events
2. **Cannot dynamically allowlist** without code change in current substrate
3. Short-term: ship a hot-fix raising `_RATE_LIMIT` on the affected route
4. Long-term: file VT-N to add a dynamic allowlist table (`twilio_rate_limit_overrides`)

## Brief-deferred follow-up

- **webhook_metrics async via DBOS workflow** (LOCK 1 option b from VT-81 review) — fire-and-forget metrics writes after returning 200 to Twilio. Reduces inline latency budget. File as VT-N before launch milestone.
- **Replay table TTL purge** — daily DELETE keeps the table bounded.
