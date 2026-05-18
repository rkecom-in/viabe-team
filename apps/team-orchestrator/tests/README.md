# team-orchestrator tests

## Running

```bash
uv run pytest                 # full suite (DB-backed tests need DATABASE_URL)
uv run pytest tests/orchestrator/test_pre_filter.py
```

Most DB-backed tests self-skip when `DATABASE_URL` is unset; the CI
`orchestrator` / `migrations` jobs provision Postgres for them.

## Integration tests (`@pytest.mark.integration`)

Tests marked `@pytest.mark.integration` make real external calls — currently
real Opus 4.7 LLM calls for the orchestrator-agent. They are **skipped by
default** and run only when explicitly opted in:

```bash
RUN_INTEGRATION_TESTS=1 ANTHROPIC_API_KEY=sk-... uv run pytest tests/orchestrator/
```

The gate is implemented in `tests/orchestrator/conftest.py`
(`pytest_collection_modifyitems`). CI does not set `RUN_INTEGRATION_TESTS`, so
integration tests never run in CI — the unit smoke tests cover the CI surface.
