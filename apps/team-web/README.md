# team-web

Viabe Team web app — Next.js 16, React 19.2.

Marketing landing page, owner dashboard, Fazal-only Ops UI, and the inbound
Twilio webhook route.

## Local dev

```bash
pnpm --filter @viabe/team-web dev      # Next.js dev server (port 3000)
pnpm --filter @viabe/team-web test     # vitest
pnpm --filter @viabe/team-web typecheck
```

Copy `.env.example` to `.env.local` and fill in. `INTERNAL_API_SECRET` must
match `apps/team-orchestrator/.env.local` exactly — generate once with
`openssl rand -hex 32` and paste the same value into both.

## Twilio webhook (VT-3.3b)

`app/api/team/twilio/webhook/route.ts` is the inbound WhatsApp ingress. It
verifies the `X-Twilio-Signature` (Twilio SDK) and forwards the raw fields to
the orchestrator (`/api/orchestrator/twilio-ingress`, signed with
`INTERNAL_API_SECRET`). Tenant lookup + rate limiting live in the orchestrator
— team-web holds no DB credentials (Pillar 8).

Full chain: **Twilio → team-web → team-orchestrator → DBOS workflow**.

## Dev Testing — Tier 2 (synthetic webhook fixtures)

Per CL-67. Fires a signed, Twilio-shaped POST at the full chain.

```bash
# 1. Start the orchestrator + team-web (each needs its .env.local).
pnpm --filter @viabe/team-web dev
uv --directory apps/team-orchestrator run uvicorn main:app --app-dir src --port 8001

# 2. Fire a synthetic webhook through team-web:
pnpm --filter @viabe/team-web exec tsx scripts/synthetic_twilio_webhook.ts \
    --tenant-phone "+919999999999" --body "STOP" --message-sid "SM_test_001"
```

`apps/team-orchestrator/scripts/synthetic_webhook.py` fires directly at the
orchestrator (skips signature verification) — use it to test the orchestrator
in isolation.
