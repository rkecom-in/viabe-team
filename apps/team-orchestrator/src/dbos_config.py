"""DBOS durable-execution configuration for the orchestrator (VT-3.1, CL-27).

DBOS is the durable substrate: every pipeline run is a ``@DBOS.workflow`` and
every state transition a ``@DBOS.step``; DBOS auto-resumes interrupted
workflows on restart. ``launch_dbos()`` is called explicitly from the
entrypoint / tests — never on import.
"""

from __future__ import annotations

import os

from dbos import DBOS, DBOSConfig

from orchestrator.graph import init_substrate, reset_substrate

# 6 minutes: the 5-minute wall-clock hard limit (concept-team.md §8.3) plus a
# 1-minute safety margin.
WORKFLOW_TIMEOUT_SECONDS = 360

_launched = False


def get_database_url() -> str:
    """Return the Postgres DSN for DBOS + the LangGraph checkpointer.

    Note: ``TEAM_SUPABASE_URL`` is the Supabase REST URL, not a Postgres DSN.
    DBOS connects to Postgres directly, so it reads ``TEAM_SUPABASE_DB_URL``
    (the direct DSN designated in apps/team-orchestrator/.env.example) or, for
    CI, ``DATABASE_URL``.
    """
    url = os.environ.get("TEAM_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "set TEAM_SUPABASE_DB_URL or DATABASE_URL (a direct Postgres DSN)"
        )
    return url


def launch_dbos() -> None:
    """Construct + launch the single DBOS instance and the LangGraph substrate.

    Idempotent within a process. On first launch DBOS auto-creates its
    ``dbos_workflows`` / ``dbos_workflow_steps`` / ``dbos_queues`` tables and
    recovers any workflows interrupted by an earlier crash.

    Scheduled-workflow registration is the CALLER's responsibility, not
    this function's. The production entrypoint
    (``main.py`` lifespan) and the substrate test fixture both call
    ``orchestrator.dbos_purge.register_purge_scheduler()`` BEFORE
    invoking ``launch_dbos`` — that ordering puts the @DBOS.scheduled
    poller in ``DBOSRegistry.pollers`` before ``_launch``
    (``_dbos.py:523``) computes the launch-time ``app_version`` hash
    at line 530 and before it drains the deferred-poller queue at
    lines 683-690. ``launch_dbos`` itself imports no scheduled-workflow
    modules; doing so here would couple the substrate to specific
    scheduler features and recreate the import-time side-effect
    problem ``register_purge_scheduler`` was introduced to avoid.
    """
    global _launched
    if _launched:
        return
    database_url = get_database_url()

    # VT-171 hot-fix (CL-56): configure Logfire BEFORE DBOS launch so the
    # OTel exporter env vars (OTEL_EXPORTER_OTLP_ENDPOINT +
    # OTEL_EXPORTER_OTLP_HEADERS) are set when DBOS starts emitting
    # workflow + step spans. No-op when LOGFIRE_TOKEN is unset.
    from orchestrator.observability.logfire import configure_logfire

    configure_logfire()

    config: DBOSConfig = {"name": "team-orchestrator", "database_url": database_url}
    DBOS(config=config)
    DBOS.launch()
    init_substrate(database_url)
    _launched = True


def shutdown_dbos() -> None:
    """Tear DBOS down. Used by tests; safe to call when not launched.

    Clears the registry-side back-reference to the destroyed DBOS
    instance — DBOS's ``_destroy`` (``_dbos.py:786-793``) clears
    ``_executor_field`` but NOT ``_launched``, and ``DBOSRegistry.dbos``
    is set in ``DBOS.__init__`` (``_dbos.py:399``) and is never reset.
    Without this manual clear, in a pytest process that cycles
    launch/destroy across module fixtures the registry continues to
    hold a stale ``dbos`` with ``_launched=True`` and
    ``_executor_field=None``. Any subsequent ``register_poller`` call
    (e.g. ``register_purge_scheduler`` BEFORE the next ``launch_dbos``
    — the architecturally correct ordering, so @DBOS.scheduled lands
    in the registry before the launch-time ``app_version`` hash and
    before ``_launch`` drains the deferred-poller queue at
    ``_dbos.py:683``) would take the ``register_poller`` "already
    launched" branch (``_dbos.py:252``) and submit to a None
    executor.
    """
    global _launched
    DBOS.destroy()
    # Clear the registry's stale back-reference so the next
    # ``register_poller`` call before launch takes the deferred branch.
    import dbos._dbos as _dbos_mod
    if _dbos_mod._dbos_global_registry is not None:
        _dbos_mod._dbos_global_registry.dbos = None
    reset_substrate()
    _launched = False
