"""VT-3.3a-fix-3 (CL-96) tests — pipeline_steps writers are replay-idempotent.

record_brain_pending / record_webhook_received use ON CONFLICT (run_id,
step_seq) DO NOTHING so a DBOS step re-execution after a crash (SQL committed
but the step not yet recorded) cannot duplicate an observability row.
(Column renamed step_index→step_seq under VT-187 / migration 025.)

Require a live Postgres via ``DATABASE_URL`` plus the dbos stack; run in the CI
``orchestrator`` job.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402 — imported after the dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — runner tests skipped",
)


@pytest.fixture(scope="module")
def runner_ctx():
    """Apply migrations + launch DBOS so the @DBOS.step writers run."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_run(dsn: str) -> tuple[str, str]:
    """Insert a tenant + pipeline_run; return (tenant_id, run_id)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-3.3a-fix-3 Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        run_id = str(uuid4())
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, str(tenant_id)),
        )
    return str(tenant_id), run_id


def _step_count(dsn: str, run_id: str, step_seq: int) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND step_seq = %s",
            (run_id, step_seq),
        ).fetchone()[0]


def test_record_brain_pending_idempotent(runner_ctx):
    """A re-executed record_brain_pending leaves exactly one row, no exception."""
    from orchestrator.runner import record_brain_pending

    tenant_id, run_id = _new_run(runner_ctx.dsn)
    record_brain_pending(tenant_id, run_id, "substantive owner message")
    record_brain_pending(tenant_id, run_id, "substantive owner message")  # replay

    assert _step_count(runner_ctx.dsn, run_id, 1) == 1


def test_record_webhook_received_idempotent(runner_ctx):
    """A re-executed record_webhook_received leaves exactly one row, no exception."""
    from orchestrator.runner import record_webhook_received

    tenant_id, run_id = _new_run(runner_ctx.dsn)
    record_webhook_received(tenant_id, run_id, {"sender_phone": "phone_tok_x"})
    record_webhook_received(tenant_id, run_id, {"sender_phone": "phone_tok_x"})  # replay

    assert _step_count(runner_ctx.dsn, run_id, 0) == 1
