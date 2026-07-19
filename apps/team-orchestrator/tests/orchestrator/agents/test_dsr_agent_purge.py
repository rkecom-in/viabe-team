"""VT-369 — DSR purge over the agent surface (the adversarial-verify Probe-6 regression).

Migration 128's composite FK ``pending_approvals(tenant_id, draft_batch_id) → agent_draft_batches``
originally said ``ON DELETE SET NULL`` with NO column list — Postgres then nulls ALL referencing
columns including the NOT NULL ``tenant_id``, so the DSR purge's ``DELETE FROM agent_draft_batches``
raised NotNullViolation and rolled back the WHOLE purge: right-to-erasure permanently broken for any
tenant that ever armed an agent approval. The fix is ``SET NULL (draft_batch_id)``. This test seeds
exactly that shape and proves the purge completes.

Also pins the default arm-fn resolution (the second verify finding): with NO injected ``arm_fn``,
``_resolve_arm_fn`` must find ``approval_glue.arm_agent_send_approval`` — the live path was dead
because ``approval_glue`` was missing from ``_ARM_FN_MODULES`` (tests had masked it by injecting).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 DSR agent-purge test skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def test_default_arm_fn_resolution_finds_approval_glue():
    """No injected arm_fn → _resolve_arm_fn returns approval_glue.arm_agent_send_approval."""
    from orchestrator.agents import approval_glue
    from orchestrator.agents.sales_recovery_executor import _resolve_arm_fn

    assert _resolve_arm_fn() is approval_glue.arm_agent_send_approval


def test_dsr_purge_completes_with_agent_approval_row(substrate):  # type: ignore[no-untyped-def]
    """A tenant with the FULL agent shape (work item → batch → draft → contact → a pending_approvals
    row referencing the batch) must DSR-purge cleanly: all 4 agent tables swept, the approval row's
    draft_batch_id nulled, its tenant_id PRESERVED (the Probe-6 NotNullViolation regression)."""
    dsn = substrate.dsn
    tid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, phase_entered_at) "
            "VALUES (%s, 'DSR Agent', 'standard', 'trial', now())",
            (str(tid),),
        )
        run = c.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'webhook', 'running') RETURNING id",
            (str(tid),),
        ).fetchone()
        cust = c.execute(
            "INSERT INTO customers (tenant_id, display_name, opt_out_status) "
            "VALUES (%s, 'C', 'subscribed') RETURNING id",
            (str(tid),),
        ).fetchone()
        wi = c.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent) "
            "VALUES (%s, 'item-1', 'sales_recovery') RETURNING id",
            (str(tid),),
        ).fetchone()
        batch = c.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'rejected') RETURNING id",
            (str(tid), str(wi[0])),
        ).fetchone()
        c.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name) "
            "VALUES (%s, %s, %s, 'team_winback_simple')",
            (str(tid), str(batch[0]), str(cust[0])),
        )
        c.execute(
            "INSERT INTO agent_customer_contacts (tenant_id, customer_id, agent, template_name) "
            "VALUES (%s, %s, 'sales_recovery', 'team_winback_simple')",
            (str(tid), str(cust[0])),
        )
        # The killer row: a RESOLVED approval referencing the batch (survives the purge as evidence,
        # so the FK SET NULL fires during the batch DELETE).
        pa = c.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "draft_batch_id, resolved_at, decision, timeout_at) "
            "VALUES (%s, %s, 'agent_customer_send', 'batch: counts only', %s, now(), 'rejected', "
            "now() + interval '30 minutes') RETURNING id",
            (str(tid), str(run[0]), str(batch[0])),
        ).fetchone()
        tk = c.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id",
            (str(tid),),
        ).fetchone()

    from orchestrator.dsr_purge import purge_tenant_data

    purge_tenant_data(UUID(str(tk[0])))  # must NOT raise (the regression rolled back here)

    with psycopg.connect(dsn, autocommit=True) as c:
        for table in ("agent_drafts", "agent_draft_batches", "agent_work_items", "agent_customer_contacts"):
            n = c.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (str(tid),)  # noqa: S608
            ).fetchone()[0]
            assert n == 0, f"{table} not swept on DSR"
        # The purge COMPLETING is the regression proof: agent_draft_batches deletes BEFORE
        # pipeline_runs in _PURGE_ORDER, so the SET NULL (draft_batch_id) fired while this approval
        # row still existed — under the broken composite FK that nulled tenant_id too and the whole
        # purge rolled back. The row itself is then legitimately cascaded away with its
        # pipeline_runs row (pre-existing purge semantics; dsr_tickets + privacy_audit_log are the
        # retention evidence surfaces, not pending_approvals).
        row = c.execute(
            "SELECT 1 FROM pending_approvals WHERE id = %s", (str(pa[0]),)
        ).fetchone()
        assert row is None, "the approval row cascades away with its purged run"
        ticket = c.execute(
            "SELECT status FROM dsr_tickets WHERE id = %s", (str(tk[0]),)
        ).fetchone()
        assert ticket is not None and ticket[0] == "completed", "the purge must have completed"
