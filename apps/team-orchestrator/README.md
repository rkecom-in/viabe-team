# team-orchestrator

Durable multi-agent workflow engine for Viabe Team.

Stack: Python 3.12, [DBOS](https://docs.dbos.dev/), LangGraph,
`langgraph_supervisor`, Anthropic SDK, Supabase + pgvector.

## Local dev

```bash
uv sync
uv run python -m team_orchestrator.main
uv run pytest
```

## Layout

- `src/team_orchestrator/` — package code (no workflows yet).
- `migrations/` — ordered, apply-once SQL migrations (`000_init.sql`, …).

DBOS workflow conventions are documented in the repo root `README.md`.
Phase 1 is a scaffold — no workflows are defined yet.
