# team-orchestrator

Durable multi-agent workflow engine for Viabe Team.

Stack: Python 3.12, [DBOS](https://docs.dbos.dev/), LangGraph,
`langgraph_supervisor`, Anthropic SDK, Supabase + pgvector.

## Local dev

```bash
uv sync
uv run pytest

# Run the FastAPI orchestrator (VT-3.3a):
uv run uvicorn main:app --app-dir src
```

## Dev Testing — Tier 2 (synthetic webhook fixtures)

Per the 3-tier dev-testing decision (CL-67), Tier 2 fires Twilio-shaped
payloads at a locally-running orchestrator — no external dependencies.

```bash
# 1. Start the orchestrator (needs TEAM_SUPABASE_DB_URL, INTERNAL_API_SECRET,
#    TEAM_PHONE_HASH_SALT in the environment — see .env.example):
uv run uvicorn main:app --app-dir src

# 2. In another shell, fire a synthetic webhook. --sender must match a
#    tenant's whatsapp_number, else the orchestrator returns unknown_sender:
uv run python scripts/synthetic_webhook.py --body "STOP" --sender "+919999999999"
```

`scripts/synthetic_webhook.py` fires directly at the orchestrator (skips Twilio
signature verification — tests the orchestrator in isolation). The orchestrator
resolves the tenant from the sender's `From` number and rate-limits before
starting the workflow.

The full inbound chain is **Twilio → team-web → team-orchestrator → DBOS**.
team-web (`apps/team-web`) owns signature verification; to exercise the whole
chain use `apps/team-web/scripts/synthetic_twilio_webhook.ts`. Tier 1 (CI) and
Tier 3 (live Twilio sandbox via ngrok, post-VT-3.4) are documented in CL-67.

## Manual Tier 2 — handler → Twilio send (VT-3.3c)

Exercises a direct handler's outbound Twilio template send end to end.

Prereqs: `TEAM_TWILIO_ACCOUNT_SID` / `TEAM_TWILIO_AUTH_TOKEN` /
`TEAM_TWILIO_FROM_NUMBER` set to Twilio sandbox values; a tenant row whose
`whatsapp_number` is your test number.

```bash
# 1. Start the orchestrator (see "Local dev" above).
uv run uvicorn main:app --app-dir src

# 2. In another shell, fire an opt-out synthetic webhook:
uv run python scripts/synthetic_webhook.py --body "STOP" --sender "<your-number>"
```

3. Verify the orchestrator logs — on a successful send:

       twilio-send: sent template 'team_opt_out_confirmation' -> phone_tok_… (sid=SM…)

   On a 4xx failure the line is `twilio-send: permanent failure template …`
   instead, and the handler still returns `send_result.success = false`
   (Pillar 7 — the send claim is never hardcoded). The recipient phone is only
   ever logged as a `phone_tok_…` token (Pillar 3).
4. Verify the Twilio Console: an outbound message using the `content_sid`
   mapped to that handler in `config/twilio_templates.yaml` (opt-out →
   `HX6365c429…`).

Handler-output persistence to `pipeline_steps` is **not** part of VT-3.3c — it
is a separate observability concern (VT-122). Verify the send via the log line
and the Twilio Console, not via a DB row.

## Layout

- `src/team_orchestrator/` — package code (no workflows yet).

Database migrations are **not** owned by this app. The shared
`viabe-team-prod` Postgres schema is managed from the repo-root
[`/migrations/`](../../migrations/) directory — see its `README.md`.

DBOS workflow conventions are documented in the repo root `README.md`.
Phase 1 is a scaffold — no workflows are defined yet.
