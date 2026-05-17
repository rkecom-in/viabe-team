"""DBOS durable-execution configuration for the orchestrator (VT-3.1, CL-27).

DBOS is the durable substrate: every pipeline run is a ``@DBOS.workflow`` and
every state transition a ``@DBOS.step``; DBOS auto-resumes interrupted
workflows on restart. ``launch_dbos()`` is called explicitly from the
entrypoint / tests — never on import.
"""

from __future__ import annotations

import os

from dbos import DBOS, DBOSConfig

from orchestrator.graph import init_substrate

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
    """
    global _launched
    if _launched:
        return
    database_url = get_database_url()
    config: DBOSConfig = {"name": "team-orchestrator", "database_url": database_url}
    DBOS(config=config)
    DBOS.launch()
    init_substrate(database_url)
    _launched = True


def shutdown_dbos() -> None:
    """Tear DBOS down. Used by tests; safe to call when not launched."""
    global _launched
    DBOS.destroy()
    _launched = False
