"""VT-311 — L2 episodic retention (18-month soft-delete) + 100K-event perf.

Asserts: (1) the retention sweep soft-deletes rows past the window and the L2 read
path (recent_events / count_events) then excludes them, while the row stays; (2) at
100K live rows, recent_events uses the partial live-rows index (the perf bound).
CL-422 synthetic.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-311 retention canary skipped",
)


def _pool():
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=2,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    return _pool()


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'vt311', 'founding', 'paid_active')", (tid,),
        )
    return tid


def test_retention_sweep_soft_deletes_old_and_reads_exclude(pool, monkeypatch):
    from orchestrator.knowledge import l2_query
    from orchestrator.knowledge.l2_retention import run_l2_retention_sweep_body

    tid = _tenant(pool)
    # One row well past the window (600 days), one fresh.
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO episodic_events (tenant_id, event_type, summary, payload, occurred_at) "
            "VALUES (%s, 'campaign_sent', 'old', '{}'::jsonb, now() - interval '600 days')", (tid,),
        )
        conn.execute(
            "INSERT INTO episodic_events (tenant_id, event_type, summary, payload, occurred_at) "
            "VALUES (%s, 'campaign_sent', 'fresh', '{}'::jsonb, now())", (tid,),
        )

    assert l2_query.count_events(tid) == 2, "both visible pre-sweep"

    monkeypatch.setenv("TEAM_L2_RETENTION_DAYS", "548")
    n = run_l2_retention_sweep_body()
    assert n >= 1, "the 600-day row must be soft-deleted"

    # Reads now exclude the soft-deleted row; the fresh one remains.
    assert l2_query.count_events(tid) == 1, "read path excludes retention-expired"
    recent = l2_query.recent_events(tid, limit=10)
    assert len(recent) == 1 and recent[0].summary == "fresh"

    # The ROW stays (audit) — present with deleted_at set when counting directly.
    with pool.connection() as conn:
        total = conn.execute(
            "SELECT count(*) AS n FROM episodic_events WHERE tenant_id = %s", (tid,),
        ).fetchone()["n"]
        soft = conn.execute(
            "SELECT count(*) AS n FROM episodic_events "
            "WHERE tenant_id = %s AND deleted_at IS NOT NULL", (tid,),
        ).fetchone()["n"]
    assert total == 2 and soft == 1, "soft-delete keeps the row (audit)"

    # Idempotent: a re-run marks nothing new.
    assert run_l2_retention_sweep_body() >= 0
    with pool.connection() as conn:
        soft2 = conn.execute(
            "SELECT count(*) AS n FROM episodic_events "
            "WHERE tenant_id = %s AND deleted_at IS NOT NULL", (tid,),
        ).fetchone()["n"]
    assert soft2 == 1, "re-run does not re-mark"


def test_recent_events_uses_live_index_at_100k(pool):
    """At 100K live rows the read path must hit the partial live-rows index, not
    a seq scan — the VT-311 perf bound."""
    tid = _tenant(pool)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO episodic_events (tenant_id, event_type, summary, payload, occurred_at) "
            "SELECT %s, 'campaign_sent', 'bulk', '{}'::jsonb, now() - (g || ' minutes')::interval "
            "FROM generate_series(1, 100000) g", (tid,),
        )
        conn.execute("ANALYZE episodic_events")
        plan = conn.execute(
            "EXPLAIN (FORMAT JSON) "
            "SELECT id FROM episodic_events WHERE tenant_id = %s AND deleted_at IS NULL "
            "ORDER BY occurred_at DESC, created_at DESC LIMIT 50", (tid,),
        ).fetchone()
    plan_text = str(plan)
    assert "idx_episodic_events_live_recent" in plan_text, f"must use the live index: {plan_text[:400]}"

    # And the live read returns promptly at scale.
    from orchestrator.knowledge import l2_query

    start = time.perf_counter()
    rows = l2_query.recent_events(tid, limit=50)
    elapsed = time.perf_counter() - start
    assert len(rows) == 50
    assert elapsed < 1.0, f"recent_events too slow at 100K: {elapsed:.3f}s"
