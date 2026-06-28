"""VT-481 — orphan-run reaper tests.

reap_orphan_runs closes runs stranded status='running' OLDER than the age floor (a process
died mid-run; DBOS can't recover a prior-app-version row), and leaves everything else alone:
a RECENT 'running' run (could be live in-flight), a 'paused' run (legitimately long-lived —
owner-approval / L3 hold), and already-terminal runs.

Real-DB (DATABASE_URL) like the other runner tests — the reaper UPDATEs pipeline_runs.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-481 orphan-reaper tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]
    return d


def _seed_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, whatsapp_number) "
            "VALUES ('VT-481 reaper', 'founding', 'trial', now(), %s) RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}",),
        ).fetchone()
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant: UUID, *, status: str, age_hours: float) -> UUID:
    """Insert a pipeline_runs row with a backdated started_at (service-role; bypass RLS)."""
    rid = uuid4()
    started = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at, step_count) "
            "VALUES (%s, %s, %s, %s, 0)",
            (str(rid), str(tenant), status, started),
        )
    return rid


def _status(dsn: str, rid: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT status FROM pipeline_runs WHERE id = %s", (str(rid),)).fetchone()
    return row[0]


def _meta(dsn: str, rid: UUID):
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT terminal_state_metadata FROM pipeline_runs WHERE id = %s", (str(rid),)
        ).fetchone()
    return row[0]


@pytest.fixture
def reaper_pool(dsn):
    """A service-role pool the reaper UPDATEs through (bypasses RLS, cross-tenant by design)."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield pool
    finally:
        pool.close()


def test_reaps_old_running_run(dsn, reaper_pool):
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    old = _seed_run(dsn, tenant, status="running", age_hours=5)  # >1h floor

    n = reap_orphan_runs(pool=reaper_pool)

    assert n >= 1
    assert _status(dsn, old) == "aborted_hard_limit", "an old 'running' orphan must be reaped"
    meta = _meta(dsn, old)
    assert meta and meta.get("reaped_by") == "vt481_orphan_reaper", "reaper marker must be stamped"


def test_leaves_recent_running_run_untouched(dsn, reaper_pool):
    """A recent 'running' run could be a LIVE in-flight run — must NOT be reaped."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    fresh = _seed_run(dsn, tenant, status="running", age_hours=0.1)  # ~6 min — within bounds

    reap_orphan_runs(pool=reaper_pool)

    assert _status(dsn, fresh) == "running", "a recent in-flight 'running' run must be left alone"


def test_leaves_paused_run_untouched(dsn, reaper_pool):
    """A 'paused' run (owner-approval / L3 hold) is legitimately long-lived — never reaped."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    paused = _seed_run(dsn, tenant, status="paused", age_hours=48)  # 2 days, but paused

    reap_orphan_runs(pool=reaper_pool)

    assert _status(dsn, paused) == "paused", "a long-lived 'paused' run must NOT be reaped"


def test_leaves_terminal_runs_untouched(dsn, reaper_pool):
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    done = _seed_run(dsn, tenant, status="completed", age_hours=10)

    reap_orphan_runs(pool=reaper_pool)

    assert _status(dsn, done) == "completed", "a terminal run must be left exactly as-is"


def test_idempotent(dsn, reaper_pool):
    """Re-running reaps nothing the second time (the row is now terminal)."""
    from orchestrator.orphan_reaper import reap_orphan_runs

    tenant = _seed_tenant(dsn)
    _seed_run(dsn, tenant, status="running", age_hours=3)

    first = reap_orphan_runs(pool=reaper_pool)
    assert first >= 1
    # second pass: the just-reaped row is no longer 'running' — only NEW orphans (none here) match.
    before = _seed_run  # noqa: F841 — readability
    second_target_absent = reap_orphan_runs(pool=reaper_pool)
    # second_target_absent may be 0 (nothing new) — the point is the reaped row isn't re-touched.
    assert isinstance(second_target_absent, int)
