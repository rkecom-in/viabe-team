"""VT-3.4 PR 3/3 — collapse-path tests (migrated to CampaignPlan v1.0 by VT-122).

Live-Postgres tests that exercise ``orchestrator.collapse.collapse_campaign_plan``
end-to-end through ``tenant_connection`` (CL-122 / Pillar 3). Mirrors the
test_tenant_isolation.py setup pattern.

Three required cases:
  1. Happy path: CampaignPlan persisted to ``campaigns`` (plan_json carries the
     v1.0 dict); ``subscriber_states`` activity fields updated.
  2. Phase-unchanged (named): ``tenants.phase`` AND the persisted
     ``subscriber_states.phase`` equal the pre-collapse phase.
  3. Cross-tenant: collapse under tenant A does not touch tenant B's rows
     (subscriber_states or campaigns).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
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


def _plan(tenant_id: str, run_id: str, *, customer_id: str | None = None):
    """Build a valid v1.0 ``proposed`` CampaignPlan for collapse tests."""
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        CampaignWindow,
        ConfidenceLevel,
        EvidenceRef,
        EvidenceSourceKind,
        ExpectedARRR,
        Language,
        MessagePlan,
        TargetCohort,
    )

    cid = UUID(customer_id) if customer_id else uuid4()
    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=UUID(tenant_id),
        run_id=UUID(run_id),
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1),
            end=now + timedelta(days=7),
        ),
        target_cohort=TargetCohort(
            customer_ids=[cid],
            cohort_label="60-90 day dormants",
            cohort_size=1,
            selection_reason="dormant cohort [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=10_000_00,
            high_paise=30_000_00,
            confidence=ConfidenceLevel.MEDIUM,
            basis="prior winback yields [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="test-evidence",
            ),
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "Owner", "discount": "10"},
            language=Language.EN,
            personalization="owner-first-name.",
        ),
    )


def test_collapse_persists_campaign_and_updates_subscriber_state(rls_ctx):
    from orchestrator.collapse import collapse_campaign_plan
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(rls_ctx.dsn)
    run_id = _new_run(rls_ctx.dsn, tenant)
    plan = _plan(tenant, run_id)

    campaign_id = collapse_campaign_plan(
        tenant_id=UUID(tenant), run_id=UUID(run_id), campaign_plan=plan
    )

    with tenant_connection(tenant) as conn:
        campaign_row = conn.execute(
            "SELECT plan_json, status, generated_at FROM campaigns WHERE id = %s",
            (str(campaign_id),),
        ).fetchone()
        sub_row = conn.execute(
            "SELECT last_campaign_at, attribution_close_pending "
            "FROM subscriber_states WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()

    assert campaign_row is not None
    # status starts at the lifecycle-initial value 'proposed' (which
    # shares the name with the agent-terminal state — see CL: status-
    # enum split). Downstream VT-6 / VT-5 flip to approved/rejected /
    # sent/failed.
    assert campaign_row["status"] == "proposed"
    assert campaign_row["generated_at"] is not None
    # plan_json carries the full v1.0 dict. Spot-check the key nested
    # fields exposed by the migration's contract.
    plan_json = campaign_row["plan_json"]
    assert plan_json["version"] == "1.0"
    assert plan_json["status"] == "proposed"
    assert plan_json["message_plan"]["template_id"] == "team_winback_v1"
    assert plan_json["message_plan"]["template_params"] == {
        "first_name": "Owner",
        "discount": "10",
    }
    assert plan_json["target_cohort"]["cohort_size"] == 1
    assert len(plan_json["evidence_refs"]) >= 1

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
        tenant_id=UUID(tenant), run_id=UUID(run_id), campaign_plan=_plan(tenant, run_id)
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
        campaign_plan=_plan(tenant_a, run_a),
    )

    with tenant_connection(tenant_b) as conn:
        b_campaigns = conn.execute("SELECT count(*) AS n FROM campaigns").fetchone()["n"]
        b_sub = conn.execute(
            "SELECT count(*) AS n FROM subscriber_states"
        ).fetchone()["n"]

    assert b_campaigns == 0, "tenant B must see no campaigns rows from tenant A's collapse"
    assert b_sub == 0, "tenant B must see no subscriber_states rows from tenant A's collapse"
