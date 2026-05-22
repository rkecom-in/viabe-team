"""DB-substrate tests for ``orchestrator.dbos_purge`` (Step-0 Branch B).

Exercises the live sweep against ``dbos.workflow_status``. Requires a
real Postgres via ``DATABASE_URL`` + the dbos stack; runs in the CI
``orchestrator`` job.

Seeds workflow_status rows directly via psycopg (no full @DBOS.workflow
invocation needed — the schema is documented at
``dbos/_schemas/system_database.py:46-91`` and direct INSERTs are
DBOS-supported for test fixtures). After the sweep:

  - Old (>retention) terminal rows (SUCCESS / ERROR) → deleted.
  - Old (>retention) PENDING / ENQUEUED / DELAYED rows → preserved
    (in-flight; recovery still needs them).
  - Recent (<retention) terminal rows → preserved.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — dbos_purge substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so its sys-DB tables exist and
    the @DBOS.scheduled poller is registered. The poller fires every
    30 min — within a fast test the scheduled invocation does NOT run;
    we call ``purge_terminal_workflow_inputs`` directly.

    DBOS provisions its own sys-DB (named ``<app_db>_dbos_sys``) —
    seeding goes through ``dbos_instance._sys_db.engine`` so we hit
    the right database + schema regardless of how DBOS was configured.
    """
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos._dbos import _get_dbos_instance
    from dbos_config import launch_dbos, shutdown_dbos

    # Register the @DBOS.scheduled purge workflow before launch so
    # the poller picks it up. main.py (production entrypoint) does
    # this for the live process; this test fixture mirrors that
    # registration so the purge_terminal_workflow_inputs call exercises
    # the registered helper. The decorator only registers the
    # scheduled func; the poller cadence (30 min) means it does not
    # fire during the test window — we call the helper directly.
    import orchestrator.dbos_purge  # noqa: F401 — registration side effect

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn, dbos=_get_dbos_instance())
    finally:
        shutdown_dbos()


def _seed_workflow_status(
    dbos_instance: Any,
    *,
    status: str,
    created_at_ms: int,
    inputs_text: str = "{}",
) -> str:
    """Insert one workflow_status row with the schema's minimum
    required columns. Returns the workflow_uuid for later assertions.

    Required-NOT-NULL columns per the schema:
      workflow_uuid (PK), created_at (BigInteger), updated_at
      (BigInteger), priority (Integer), was_forked_from (Boolean),
      rate_limited (Boolean). Everything else nullable.

    Writes via the DBOS sys-DB engine so the test hits the right
    database + the schema that DBOS configured at init.
    """
    from dbos._schemas.system_database import SystemSchema

    wfid = f"test-purge-{uuid4().hex}"
    ws = SystemSchema.workflow_status
    with dbos_instance._sys_db.engine.begin() as conn:
        conn.execute(
            ws.insert().values(
                workflow_uuid=wfid,
                status=status,
                created_at=created_at_ms,
                updated_at=created_at_ms,
                priority=0,
                was_forked_from=False,
                rate_limited=False,
                inputs=inputs_text,
            )
        )
    return wfid


def _exists(dbos_instance: Any, wfid: str) -> bool:
    from sqlalchemy import select

    from dbos._schemas.system_database import SystemSchema

    ws = SystemSchema.workflow_status
    with dbos_instance._sys_db.engine.begin() as conn:
        row = conn.execute(
            select(ws.c.workflow_uuid).where(ws.c.workflow_uuid == wfid)
        ).fetchone()
    return row is not None


# --- Tests ------------------------------------------------------------------


def test_old_terminal_rows_are_purged(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """SUCCESS / ERROR / CANCELLED / MAX_RECOVERY_ATTEMPTS_EXCEEDED rows
    older than the retention SLA are deleted by one sweep."""
    from orchestrator.dbos_purge import (
        _RETENTION_ENV_VAR,
        purge_terminal_workflow_inputs,
    )

    # Shrink retention so the "old" rows we seed actually fall behind
    # the cutoff in a fast test. 1 second retention.
    monkeypatch.setenv(_RETENTION_ENV_VAR, "1")

    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 60_000  # 60 s ago — older than 1 s retention

    # Seed one row in each terminal state at an "old" timestamp.
    old_success = _seed_workflow_status(
        substrate.dbos, status="SUCCESS", created_at_ms=old_ms,
        inputs_text='{"args":[],"kwargs":{"Body":"OLD_SECRET_PROBE_1"}}',
    )
    old_error = _seed_workflow_status(
        substrate.dbos, status="ERROR", created_at_ms=old_ms,
        inputs_text='{"args":[],"kwargs":{"Body":"OLD_SECRET_PROBE_2"}}',
    )
    old_cancelled = _seed_workflow_status(
        substrate.dbos, status="CANCELLED", created_at_ms=old_ms,
    )
    old_max_retry = _seed_workflow_status(
        substrate.dbos,
        status="MAX_RECOVERY_ATTEMPTS_EXCEEDED",
        created_at_ms=old_ms,
    )

    cutoff_ms, deleted = purge_terminal_workflow_inputs()

    assert cutoff_ms < now_ms  # cutoff is in the past
    assert deleted >= 4  # at least our four seeded rows
    assert not _exists(substrate.dbos, old_success)
    assert not _exists(substrate.dbos, old_error)
    assert not _exists(substrate.dbos, old_cancelled)
    assert not _exists(substrate.dbos, old_max_retry)


def test_in_flight_rows_are_preserved_regardless_of_age(
    substrate, monkeypatch
):  # type: ignore[no-untyped-def]
    """PENDING / ENQUEUED / DELAYED rows — even ones older than the
    retention SLA — must NOT be deleted. Recovery still needs them."""
    from orchestrator.dbos_purge import (
        _RETENTION_ENV_VAR,
        purge_terminal_workflow_inputs,
    )

    monkeypatch.setenv(_RETENTION_ENV_VAR, "1")

    now_ms = int(time.time() * 1000)
    ancient_ms = now_ms - 86_400_000  # 24 h ago — extremely old

    old_pending = _seed_workflow_status(
        substrate.dbos, status="PENDING", created_at_ms=ancient_ms,
        inputs_text='{"args":[],"kwargs":{"Body":"PENDING_BODY_PROBE"}}',
    )
    old_enqueued = _seed_workflow_status(
        substrate.dbos, status="ENQUEUED", created_at_ms=ancient_ms,
    )
    old_delayed = _seed_workflow_status(
        substrate.dbos, status="DELAYED", created_at_ms=ancient_ms,
    )

    purge_terminal_workflow_inputs()

    assert _exists(substrate.dbos, old_pending), (
        "PENDING row was purged — recovery would lose the workflow's inputs"
    )
    assert _exists(substrate.dbos, old_enqueued), (
        "ENQUEUED row was purged — queue worker would lose the dispatch"
    )
    assert _exists(substrate.dbos, old_delayed), (
        "DELAYED row was purged — delayed dispatch would never fire"
    )


def test_recent_terminal_rows_within_retention_are_preserved(
    substrate, monkeypatch
):  # type: ignore[no-untyped-def]
    """Within the retention SLA, terminal rows are NOT deleted yet —
    the operator may need them for debugging within the window."""
    from orchestrator.dbos_purge import (
        _RETENTION_ENV_VAR,
        purge_terminal_workflow_inputs,
    )

    # 1-hour retention — recent rows from "now" are well inside.
    monkeypatch.setenv(_RETENTION_ENV_VAR, "3600")
    now_ms = int(time.time() * 1000)

    recent_success = _seed_workflow_status(
        substrate.dbos, status="SUCCESS", created_at_ms=now_ms,
    )
    recent_error = _seed_workflow_status(
        substrate.dbos, status="ERROR", created_at_ms=now_ms,
    )

    purge_terminal_workflow_inputs()

    assert _exists(substrate.dbos, recent_success)
    assert _exists(substrate.dbos, recent_error)


def test_zero_row_sweep_is_idempotent(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """A sweep over an empty / no-old-rows table must not raise. Locks
    the "concurrent + zero-row safety" contract from the brief."""
    from orchestrator.dbos_purge import (
        _RETENTION_ENV_VAR,
        purge_terminal_workflow_inputs,
    )

    # 1-week retention — there should be no rows that old in a fresh
    # CI DB.
    monkeypatch.setenv(_RETENTION_ENV_VAR, str(7 * 86400))

    cutoff_ms, deleted = purge_terminal_workflow_inputs()
    assert deleted >= 0  # 0 expected on a fresh CI run; not strict
    # Idempotency — second call against the same window also clean.
    cutoff_ms_2, deleted_2 = purge_terminal_workflow_inputs()
    assert cutoff_ms_2 >= cutoff_ms
    assert deleted_2 >= 0
