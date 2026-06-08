"""DBOS durable-execution configuration for the orchestrator (VT-3.1, CL-27).

DBOS is the durable substrate: every pipeline run is a ``@DBOS.workflow`` and
every state transition a ``@DBOS.step``; DBOS auto-resumes interrupted
workflows on restart. ``launch_dbos()`` is called explicitly from the
entrypoint / tests — never on import.
"""

from __future__ import annotations

import logging
import os

from dbos import DBOS, DBOSConfig

from orchestrator.graph import init_substrate, reset_substrate

logger = logging.getLogger(__name__)

# 6 minutes: the 5-minute wall-clock hard limit (concept-team.md §8.3) plus a
# 1-minute safety margin.
WORKFLOW_TIMEOUT_SECONDS = 360

# DBOS app name. Fazal registered the Conductor app as "viabe-team" (console.dbos.dev); DBOS binds the
# Conductor websocket + console by this name (the bind URL is .../websocket/<app_name>/<key>), so it
# MUST match the registered name. Env-overridable for prod/staging. NOTE (VT-161 due-diligence): the
# app name is a Conductor/console identifier ONLY — it is NOT written to the workflow/queue tables and
# is NOT in any recovery WHERE clause. Self-hosted recovery keys on the Postgres DB + executor_id +
# application_version (the workflow-source hash, which the name does not feed). So renaming
# team-orchestrator→viabe-team does NOT orphan in-flight workflows and needs no prod drain.
_DEFAULT_APP_NAME = "viabe-team"

_launched = False


def _build_dbos_config(database_url: str) -> DBOSConfig:
    """Assemble the DBOSConfig.

    App name = ``DBOS_APPLICATION_NAME`` (default ``viabe-team``). Conductor is OPT-IN and
    NON-CRITICAL (DBOS docs): ``DBOS_CONDUCTOR_KEY`` present → connect to Conductor on a background
    thread; absent → local-recovery only. The connection runs on a daemon thread started inside
    ``DBOS.launch()``, so a missing OR unreachable/invalid key never blocks or crashes launch — it
    degrades to local recovery. We log which mode at startup.
    """
    name = os.environ.get("DBOS_APPLICATION_NAME", _DEFAULT_APP_NAME) or _DEFAULT_APP_NAME
    config: DBOSConfig = {"name": name, "database_url": database_url}
    conductor_key = (os.environ.get("DBOS_CONDUCTOR_KEY") or "").strip()
    if conductor_key:
        config["conductor_key"] = conductor_key
        logger.info("DBOS launching WITH Conductor (DBOS_CONDUCTOR_KEY present) — app=%s", name)
    else:
        logger.info("DBOS launching local-recovery only (no DBOS_CONDUCTOR_KEY) — app=%s", name)
    return config


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

    # VT-179 boot hook (CL-419 / VT-179 fix-1): validate the typed-envelope
    # registry covers every step_kind=<literal> in source. Fail-fast at
    # orchestrator-process boot so unregistered envelopes cannot reach
    # production. Lives here (not in orchestrator/__init__.py) because the
    # package-level import path traverses observability/__init__.py's eager
    # re-exports, which pull psycopg — unacceptable for minimal-deps CI
    # test runs that import orchestrator without launching DBOS.
    from orchestrator.observability.envelopes import (
        validate_registry_completeness,
    )

    validate_registry_completeness()

    # VT-181: every @tool_step decorator's step_kind must be in
    # STEP_KIND_REGISTRY (envelope-type drift is OUT of scope per
    # docstring; payload-shape is per-tool free-form JSONB).
    # Triggers import of any modules that define decorated tools so
    # TOOL_STEP_REGISTRY is populated before the check runs.
    import orchestrator.agent.tools.compose_output  # noqa: F401
    from orchestrator.observability.decorators import (
        validate_tool_step_registry,
    )

    validate_tool_step_registry()

    database_url = get_database_url()

    # VT-171 hot-fix (CL-56): configure Logfire BEFORE DBOS launch so the
    # OTel exporter env vars (OTEL_EXPORTER_OTLP_ENDPOINT +
    # OTEL_EXPORTER_OTLP_HEADERS) are set when DBOS starts emitting
    # workflow + step spans. No-op when LOGFIRE_TOKEN is unset.
    from orchestrator.observability.logfire import configure_logfire

    configure_logfire()

    config = _build_dbos_config(database_url)
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
