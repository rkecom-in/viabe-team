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
    from orchestrator.dbos_purge import register_purge_scheduler

    # Register scheduled workflows BEFORE launch_dbos so their
    # @DBOS.scheduled decoration lands in the registry before the
    # poller threads start. ``register_purge_scheduler`` is an
    # explicit call — importing ``orchestrator.dbos_purge`` has no
    # registration side effect, so test fixtures that import the
    # module purely for ``purge_terminal_workflow_inputs`` do not
    # accidentally poison the DBOS registry. This isolation matters:
    # adding registry entries shifts ``app_version`` per
    # ``DBOSRegistry.compute_app_version``, and a shift between
    # processes (e.g. subprocess workers vs. parent fixture) would
    # break the recovery filter at ``_recovery.py:58``
    # (``get_pending_workflows(executor_id, app_version)``).
    register_purge_scheduler()

    launch_dbos()
    yield
    shutdown_dbos()


app = FastAPI(title="Viabe Team Orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up and the API is mounted."""
    return {"status": "ok"}
