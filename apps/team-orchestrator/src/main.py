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

    launch_dbos()
    # Register scheduled workflows AFTER launch_dbos. DBOS's
    # ``DBOSRegistry.register_poller`` (_dbos.py:249-256) branches on
    # ``self.dbos._launched``: when launched, it submits to
    # ``self.dbos._executor`` immediately; when not launched, it
    # queues the poller for DBOS.launch to start. The registry-side
    # ``dbos`` reference is set during DBOS.__init__ and NOT cleared by
    # DBOS.destroy, so in a process that has cycled launch + destroy
    # (e.g. pytest re-launching across fixtures) the registry can hold a
    # stale dbos with ``_launched=True`` and ``_executor_field=None``.
    # Calling register AFTER a fresh launch ensures the registry's
    # ``dbos`` reference is current and the executor is available.
    # ``register_purge_scheduler`` is also an explicit call (importing
    # ``orchestrator.dbos_purge`` has no registration side effect), so
    # test fixtures that import the module purely for
    # ``purge_terminal_workflow_inputs`` do not poison the DBOS
    # registry: adding registry entries shifts ``app_version`` per
    # ``DBOSRegistry.compute_app_version``, and a shift between
    # processes (e.g. subprocess workers vs. parent fixture) would
    # break the recovery filter at ``_recovery.py:58``
    # (``get_pending_workflows(executor_id, app_version)``).
    register_purge_scheduler()
    yield
    shutdown_dbos()


app = FastAPI(title="Viabe Team Orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up and the API is mounted."""
    return {"status": "ok"}
