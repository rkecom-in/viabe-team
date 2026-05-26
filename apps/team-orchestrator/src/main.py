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
    from orchestrator.scheduled_triggers import register_scheduled_triggers

    # Register scheduled workflows BEFORE launch_dbos so the registered
    # set is in the registry when ``_launch`` (``_dbos.py:523``) computes
    # the launch-time ``app_version`` hash (line 530:
    # ``GlobalParams.app_version = self._registry.compute_app_version()``)
    # and when ``_launch`` drains the deferred-poller queue at
    # ``_dbos.py:683``. ``register_purge_scheduler`` is an explicit
    # call — importing ``orchestrator.dbos_purge`` has no registration
    # side effect, so test fixtures that import the module purely for
    # ``purge_terminal_workflow_inputs`` do not poison the DBOS
    # registry. Cross-process consistency: every process that runs
    # main.py registers in the same order before launch, so the
    # launch-time ``app_version`` hash includes the purge workflow on
    # every process, and the recovery filter at ``_recovery.py:58``
    # (``get_pending_workflows(executor_id, app_version)``) matches.
    #
    # Pytest-fixture isolation: ``shutdown_dbos`` clears
    # ``_dbos_global_registry.dbos`` so the next ``register_poller``
    # call (this one, on the next process's lifespan or the next
    # pytest fixture's launch) takes the deferred-poller branch
    # (``_dbos.py:256``) instead of submitting to the destroyed
    # instance's None executor.
    register_purge_scheduler()
    # VT-28: 4 scheduled trigger workflows. Same register-before-launch
    # contract as register_purge_scheduler — see scheduled_triggers.py
    # docstring for the DBOS app_version invariant.
    register_scheduled_triggers()
    launch_dbos()
    yield
    shutdown_dbos()


app = FastAPI(title="Viabe Team Orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up and the API is mounted."""
    return {"status": "ok"}
