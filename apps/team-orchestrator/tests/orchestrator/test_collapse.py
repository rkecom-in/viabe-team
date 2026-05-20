"""VT-3.4 PR 3/3 — collapse-path tests.

Live-Postgres tests that exercise ``orchestrator.collapse.collapse_campaign_plan``
end-to-end through ``tenant_connection`` (CL-122 / Pillar 3). Mirrors the
test_tenant_isolation.py setup pattern.

Three required cases:
  1. Happy path: CampaignPlan persisted to ``campaigns``; ``subscriber_states``
     activity fields updated.
  2. Phase-unchanged (named): ``tenants.phase`` AND the persisted
     ``subscriber_states.phase`` equal the pre-collapse phase.
  3. Cross-tenant: collapse under tenant A does not touch tenant B's rows
     (subscriber_states or campaigns).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("pydantic")

import psycopg  # noqa: E402 — imported after the dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — collapse tests skipped",
)


@pytest.fixture(scope="module")
def rls_ctx():
    """Apply migrations (incl. 016/017) + launch DBOS so the pool exists."""
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


def _new_tenant(dsn: str, *, phase: str = "paid_at_risk") -> str:
    """Seed a tenant via a direct superuser connection (RLS bypassed)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES (%s, 'founding', %s) RETURNING id",
            ("collapse-test", phase),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _new_run(dsn: str, tenant_id: str) -> str:
    """Seed a pipeline_runs row (FK target for campaigns.run_id)."""
    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_id),
        )
    return run_id


def _plan(tenant_id: str, *, subscriber_id: str | None = None):
    from orchestrator.types.campaign_plan import CampaignPlan

    return CampaignPlan(
        tenant_id=UUID(tenant_id),
        subscriber_id=UUID(subscriber_id) if subscriber_id else uuid4(),
        template_id="team_winback_v1",
        body_params={"name": "Owner", "days_inactive": "14"},
        status="proposed",
        proposed_at=datetime.now(UTC),
        proposed_by="sales_recovery_agent",
    )


def test_collapse_persists_campaign_and_updates_subscriber_state(rls_ctx):
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant)

    campaign_id = collapse_campaign_plan(
        tenant_id=UUID(tenant), run_id=UUID(run_id), campaign_plan=plan
    )

    with tenant_connection(tenant) as conn:
        campaign_row = conn.execute(
            "SELECT template_id, status, proposed_by FROM campaigns WHERE id = %s",
            (str(campaign_id),),
        ).fetchone()
        sub_row = conn.execute(
            "SELECT last_campaign_at, attribution_close_pending "
            "FROM subscriber_states WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()

    assert campaign_row is not None
    assert campaign_row["template_id"] == "team_winback_v1"
    assert campaign_row["status"] == "proposed"
    assert campaign_row["proposed_by"] == "sales_recovery_agent"

    assert sub_row is not None
    assert sub_row["last_campaign_at"] is not None
    assert sub_row["attribution_close_pending"] == [campaign_id]


def test_collapse_does_not_change_phase(rls_ctx):
    """Required (CL-233): collapse path is activity-only; phase is untouched."""
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    starting_phase = "paid_at_risk"
    tenant = _new_tenant(rls_ctx.dsn, phase=starting_phase)
    run_id = _new_run(rls_ctx.dsn, tenant)

    with tenant_connection(tenant) as conn:
        phase_before = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()["phase"]

    collapse_campaign_plan(
        tenant_id=UUID(tenant), run_id=UUID(run_id), campaign_plan=_plan(tenant)
    )

    with tenant_connection(tenant) as conn:
        tenants_phase_after = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()["phase"]
        sub_phase = conn.execute(
            "SELECT phase FROM subscriber_states WHERE tenant_id = %s", (tenant,)
        ).fetchone()["phase"]

    assert phase_before == starting_phase
    assert tenants_phase_after == starting_phase, "collapse must not mutate tenants.phase"
    assert sub_phase == starting_phase, "subscriber_states.phase must mirror tenants.phase"


def test_collapse_does_not_cross_tenant_boundary(rls_ctx):
    """Tenant A's collapse must not touch tenant B's campaigns / subscriber_states."""
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    tenant_a = _new_tenant(rls_ctx.dsn)
    tenant_b = _new_tenant(rls_ctx.dsn)
    run_a = _new_run(rls_ctx.dsn, tenant_a)

    collapse_campaign_plan(
        tenant_id=UUID(tenant_a),
        run_id=UUID(run_a),
        campaign_plan=_plan(tenant_a),
    )

    with tenant_connection(tenant_b) as conn:
        b_campaigns = conn.execute("SELECT count(*) AS n FROM campaigns").fetchone()["n"]
        b_sub = conn.execute(
            "SELECT count(*) AS n FROM subscriber_states"
        ).fetchone()["n"]

    assert b_campaigns == 0, "tenant B must see no campaigns rows from tenant A's collapse"
    assert b_sub == 0, "tenant B must see no subscriber_states rows from tenant A's collapse"
