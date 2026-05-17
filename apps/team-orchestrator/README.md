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

## Layout

- `src/team_orchestrator/` — package code (no workflows yet).

Database migrations are **not** owned by this app. The shared
`viabe-team-prod` Postgres schema is managed from the repo-root
[`/migrations/`](../../migrations/) directory — see its `README.md`.

DBOS workflow conventions are documented in the repo root `README.md`.
Phase 1 is a scaffold — no workflows are defined yet.
