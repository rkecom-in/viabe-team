"""VT-307 — nightly KG-events outbox-drain straggler sweep canary.

The handler re-drains undrained kg_events across active tenants; a tenant whose
drain reports failures (`failed > 0` — an event the drain can't project, e.g. an
unknown event_type) gets a per-tenant `kg_drain_straggler` warning. Asserts: a
straggler tenant → the alert persists; a clean tenant (nothing undrained) → none.
CL-422 synthetic; canary-tenant env so no real send fires.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-307 KG-drain sweep canary skipped",
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


def _active_tenant(pool) -> str:
    """A tenant with a recent pipeline_run → counts as active for the sweep."""
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'vt307', 'founding', 'paid_active')", (tid,),
        )
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status, started_at) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed', now())",
            (str(uuid4()), tid),
        )
    return tid


def _alerts(pool, tid: str, kind: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM tenant_alerts "
            "WHERE tenant_id = %s AND trigger_kind = %s", (tid, kind),
        ).fetchone()
    return int(row["n"])


def test_kg_drain_sweep_alerts_on_straggler_not_on_clean(pool, monkeypatch):
    from orchestrator.scheduled_triggers import kg_drain_sweep_scheduled

    # Straggler tenant: active + an UNDRAINABLE outbox event (unknown event_type →
    # process_kg_event returns 'failed' → drain_kg_events failed=1).
    tid_bad = _active_tenant(pool)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO kg_events (event_id, event_type, tenant_id, payload) "
            "VALUES (%s, %s, %s, '{}'::jsonb)",
            (str(uuid4()), "vt307_unknown_type", tid_bad),
        )
    # Clean tenant: active, no undrained kg_events.
    tid_clean = _active_tenant(pool)

    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", f"{tid_bad},{tid_clean}")
    now = datetime.now(UTC)
    kg_drain_sweep_scheduled(now, now)

    assert _alerts(pool, tid_bad, "kg_drain_straggler") >= 1, "a drain straggler must alert"
    assert _alerts(pool, tid_clean, "kg_drain_straggler") == 0, "a clean tenant must not alert"
