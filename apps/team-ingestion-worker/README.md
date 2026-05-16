# team-ingestion-worker

Heavy data ingestion for Viabe Team.

Stack: Python 3.12, [Apify SDK](https://docs.apify.com/sdk/python/),
Sarvam AI client, Anthropic SDK.

## Local dev

```bash
uv sync
uv run python -m team_ingestion_worker.main
uv run pytest
```

Phase 1 is a scaffold — no ingestion pipelines are defined yet.
