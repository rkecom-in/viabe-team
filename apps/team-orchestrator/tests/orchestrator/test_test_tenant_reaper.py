"""VT-620 — test-tenant reaper + FK-safe delete tests.

``reap_test_tenants`` FK-safely deletes leaked ``convo-harness-…`` tenants (+ their runs/steps)
OLDER than the 1h floor, and leaves everything else alone: a real-named tenant (never in scope)
and a RECENT convo-harness tenant (could be a live harness run). ``fk_safe_delete_tenant`` deletes
pipeline_steps FIRST (the non-cascade FK that blocked the old teardown) and reports any residual.

Real-DB (DATABASE_URL) like the sibling orphan-reaper tests.
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
    reason="DATABASE_URL not set — VT-620 test-tenant reaper tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]
    return d


def _seed_tenant(dsn: str, *, name: str, age_hours: float) -> UUID:
    """Insert a tenant with a backdated created_at (service-role; bypass RLS)."""
    tid = uuid4()
    created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, created_at) "
            "VALUES (%s, %s, 'founding', 'trial', %s)",
            (str(tid), name, created),
        )
    return tid


def _seed_run_with_step(dsn: str, tenant: UUID) -> UUID:
    """A run + a pipeline_step — the exact non-cascade FK shape the old teardown leaked."""
    rid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (str(rid), str(tenant)),
        )
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, status) "
            "VALUES (%s, %s, 0, 'error', 'completed')",
            (str(rid), str(tenant)),
        )
    return rid


def _tenant_exists(dsn: str, tid: UUID) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM tenants WHERE id = %s", (str(tid),)).fetchone()
    return row is not None


@pytest.fixture
def reaper_pool(dsn):
    """A service-role pool the reaper deletes through (bypasses RLS, cross-tenant by design)."""
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


def test_reaps_old_harness_tenant_and_its_rows(dsn, reaper_pool):
    from orchestrator.test_tenant_reaper import reap_test_tenants

    tid = _seed_tenant(dsn, name=f"convo-harness-{uuid4().hex[:8]}", age_hours=5)  # >1h floor
    rid = _seed_run_with_step(dsn, tid)

    n = reap_test_tenants(pool=reaper_pool)

    assert n >= 1
    assert not _tenant_exists(dsn, tid), "an old convo-harness tenant must be reaped"
    # The FK-blocking rows (pipeline_steps + pipeline_runs) must be gone too.
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM pipeline_runs WHERE id = %s", (str(rid),)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM pipeline_steps WHERE run_id = %s", (str(rid),)
        ).fetchone() is None


def test_leaves_real_named_tenant_untouched(dsn, reaper_pool):
    """A real-named tenant is NEVER in scope — strict convo-harness-% pattern only."""
    from orchestrator.test_tenant_reaper import reap_test_tenants

    tid = _seed_tenant(dsn, name=f"Sharma Sweets {uuid4().hex[:6]}", age_hours=48)
    reap_test_tenants(pool=reaper_pool)
    assert _tenant_exists(dsn, tid), "a real-named tenant must never be reaped"


def test_leaves_recent_harness_tenant_untouched(dsn, reaper_pool):
    """A RECENT convo-harness tenant could be a live harness run — the 1h floor protects it."""
    from orchestrator.test_tenant_reaper import reap_test_tenants

    tid = _seed_tenant(dsn, name=f"convo-harness-{uuid4().hex[:8]}", age_hours=0.08)  # ~5 min
    reap_test_tenants(pool=reaper_pool)
    assert _tenant_exists(dsn, tid), "a recent (<1h) convo-harness tenant must NOT be reaped"


def test_fk_safe_delete_tenant_clears_steps_first(dsn, reaper_pool):
    """Direct unit: fk_safe_delete_tenant fully deletes a tenant with runs+steps, no residual."""
    from orchestrator.test_tenant_reaper import fk_safe_delete_tenant

    tid = _seed_tenant(dsn, name=f"convo-harness-{uuid4().hex[:8]}", age_hours=5)
    _seed_run_with_step(dsn, tid)

    with reaper_pool.connection() as conn:
        blocked = fk_safe_delete_tenant(conn, str(tid))

    assert blocked == [], f"expected a clean delete, still blocked by: {blocked}"
    assert not _tenant_exists(dsn, tid)
