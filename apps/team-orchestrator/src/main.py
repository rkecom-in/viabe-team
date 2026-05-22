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

    # Register scheduled workflows BEFORE launch_dbos so their
    # @DBOS.scheduled decorators land in the registry before the
    # poller threads start. Importing this module here (and NOT from
    # launch_dbos itself) keeps the production process getting the
    # purge while keeping the keyless test fixtures that call
    # launch_dbos directly free of the @DBOS.scheduled registration
    # — adding registry entries shifts ``app_version`` per
    # ``DBOSRegistry.compute_app_version``, which would break the
    # cross-process app_version match that DBOS's recovery filter
    # relies on (see _recovery.py:58, get_pending_workflows filters
    # by app_version).
    import orchestrator.dbos_purge  # noqa: F401 — registration side effect

    launch_dbos()
    yield
    shutdown_dbos()


app = FastAPI(title="Viabe Team Orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up and the API is mounted."""
    return {"status": "ok"}
