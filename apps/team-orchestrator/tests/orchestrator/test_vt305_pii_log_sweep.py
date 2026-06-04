"""VT-305 — nightly PII-in-log sweep scheduled handler canary.

The handler runs VT-79 Detector-5 (detect_pii_in_logs) across active tenants and
dispatches a per-tenant CRITICAL pii_in_log alert for each finding. Asserts: a
pipeline_step carrying unredacted PII → the sweep persists a pii_in_log
tenant_alerts row; a clean tenant → none. CL-422 synthetic; canary-tenant env so
no real send fires. (Registration/idempotency covered by test_scheduled_triggers.)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-305 PII-sweep canary skipped",
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
            "VALUES (%s, 'vt305', 'founding', 'paid_active')", (tid,),
        )
    return tid


def _seed_run_with_step(pool, tid: str, input_envelope: str) -> None:
    """A recent pipeline_run (makes the tenant 'active') + a pipeline_step whose
    input_envelope carries the given JSON (PII or clean)."""
    run_id = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status, started_at) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed', now())",
            (run_id, tid),
        )
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, status, input_envelope, started_at) "
            "VALUES (%s, %s, 0, 'webhook_received', 'completed', %s::jsonb, now())",
            (run_id, tid, input_envelope),
        )


def _alerts(pool, tid: str, kind: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM tenant_alerts "
            "WHERE tenant_id = %s AND trigger_kind = %s", (tid, kind),
        ).fetchone()
    return int(row["n"])


def test_pii_log_sweep_alerts_on_pii_not_on_clean(pool, monkeypatch):
    from orchestrator.scheduled_triggers import pii_log_sweep_scheduled

    # Tenant A: a step with an unredacted phone number in the envelope.
    tid_pii = _tenant(pool)
    _seed_run_with_step(pool, tid_pii, '{"body": "please call +919876543210 today"}')
    # Tenant B: a clean step (no PII).
    tid_clean = _tenant(pool)
    _seed_run_with_step(pool, tid_clean, '{"status": "ok", "step": "received"}')

    # Canary tenants → DEV bot, empty token → no real send; the persist still proves it.
    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", f"{tid_pii},{tid_clean}")

    now = datetime.now(UTC)
    pii_log_sweep_scheduled(now, now)

    assert _alerts(pool, tid_pii, "pii_in_log") >= 1, "PII in a log payload must raise pii_in_log"
    assert _alerts(pool, tid_clean, "pii_in_log") == 0, "a clean tenant must not alert"
