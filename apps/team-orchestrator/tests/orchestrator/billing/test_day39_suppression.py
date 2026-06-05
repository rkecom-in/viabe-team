"""VT-92 — day-39 90-day-suppression + decision persistence canary (real PG).

Suppression: a CONTINUE suppresses re-evaluation for 90 days, then the tenant is
re-eligible; a refund_triggered is terminal. Persistence: the verdict lands in
day39_evaluations. Deterministic, zero-LLM.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-92 suppression canary skipped",
)

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield graph_mod._pool
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev


def _paid_tenant(pool) -> str:
    tid = str(uuid.uuid4())
    with pool.connection() as c:
        c.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, paid_conversion_at) "
            "VALUES (%s, 'p', 'founding', 'paid_active', %s)",
            (tid, _NOW - timedelta(days=40)),
        )
    return tid


def _log_day39(pool, tid, event_type, *, created_at):
    with pool.connection() as c:
        c.execute(
            "INSERT INTO pipeline_log (run_id, event_type, tenant_id, severity, component, "
            "payload, created_at) VALUES (%s, %s, %s, 'info', 'day39', '{}'::jsonb, %s)",
            (str(uuid.uuid4()), event_type, tid, created_at),
        )


def test_suppression_window(pool):
    from orchestrator.scheduled_triggers import _scan_day39_eligible

    fresh = _paid_tenant(pool)                       # no prior event → eligible
    cont_recent = _paid_tenant(pool)
    _log_day39(pool, cont_recent, "day39_continue", created_at=_NOW - timedelta(days=60))
    cont_old = _paid_tenant(pool)
    _log_day39(pool, cont_old, "day39_continue", created_at=_NOW - timedelta(days=91))
    refunded = _paid_tenant(pool)
    _log_day39(pool, refunded, "day39_refund_triggered", created_at=_NOW - timedelta(days=200))

    eligible = {str(t) for t in _scan_day39_eligible(_NOW)}
    assert fresh in eligible
    assert cont_recent not in eligible, "continue within 90d must suppress"
    assert cont_old in eligible, "continue >90d ago → re-eligible"
    assert refunded not in eligible, "refund is terminal — never re-evaluate"


def test_persist_writes_day39_evaluation(pool):
    from types import SimpleNamespace

    from orchestrator.scheduled_triggers import _persist_day39_evaluation

    tid = _paid_tenant(pool)
    verdict = SimpleNamespace(
        verdict="continue", arrr_paise=10000, cumulative_fees_paise=5000,
        already_decided=False,
    )
    _persist_day39_evaluation(tid, verdict)

    with pool.connection() as c:
        row = c.execute(
            "SELECT verdict, arrr_paise, cumulative_fees_paise, evaluator_version "
            "FROM day39_evaluations WHERE tenant_id = %s", (tid,),
        ).fetchone()
    assert row is not None
    assert row["verdict"] == "continue"
    assert row["arrr_paise"] == 10000
    assert row["cumulative_fees_paise"] == 5000
    assert row["evaluator_version"] == "1.0.0"
