"""FastAPI entrypoint for the Viabe Team orchestrator (VT-3.3a).

Run locally:  uvicorn main:app --app-dir src

The lifespan launches DBOS on startup — never on import (DBOS connects to
Postgres and recovers interrupted workflows on launch).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.api import router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    yield
    shutdown_dbos()


app = FastAPI(title="Viabe Team Orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up and the API is mounted."""
    return {"status": "ok"}
