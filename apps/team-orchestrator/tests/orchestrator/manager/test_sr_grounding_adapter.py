"""VT-607 (Loop Package 6) — the typed CampaignPlan-variant -> PlanSpecialistReturn adapter
(``manager.review.adapt_campaign_plan_to_specialist_return``) and its deterministic grounding
check (``_cohort_ids_are_grounded``). Live Postgres (the grounding check does a REAL, read-only
customers existence query) — no LLM call anywhere in this file; the adapter is deterministic by
design.

Covers Package 6's acceptance criterion "rejected/revised/failed plan variants return to the
Manager": each of the three CampaignPlan variants (+ the ungrounded-cohort case) is proven to map,
through the EXISTING to_legacy_specialist_return bridge + decide_next_action, onto the correct
Manager decision (ESCALATE for out_of_scope/ungrounded-cohort — no proposed path; REVISE for
insufficient_data — the Manager can reframe or wait; ACCEPT/NEXT_SPECIALIST for a grounded
proposed plan).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — SR grounding/adapter DB tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant_with_customers(pool, n: int) -> tuple[str, list]:
    from uuid import UUID

    tid = str(uuid4())
    customer_ids: list[UUID] = []
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"srg-{tid[:8]}"),
        )
        for i in range(n):
            row = conn.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, source) "
                "VALUES (%s, %s, %s, 'subscribed', 'test') RETURNING id",
                (tid, f"Customer {i}", f"+9199{uuid4().int % 10**8:08d}"),
            ).fetchone()
            customer_ids.append(row["id"] if isinstance(row, dict) else row[0])
    return tid, customer_ids


def _proposed_plan(*, tenant_id, run_id, customer_ids):
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

    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=tenant_id,
        run_id=run_id,
        generated_at=now,
        campaign_window=CampaignWindow(start=now + timedelta(minutes=5), end=now + timedelta(days=7)),
        target_cohort=TargetCohort(
            customer_ids=list(customer_ids),
            cohort_label="dormant_test_cohort",
            cohort_size=len(customer_ids),
            selection_reason="Selected via test fixture [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=100_000, high_paise=500_000,
            confidence=ConfidenceLevel.MEDIUM, basis="Test projection [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1", source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="test-evidence-1",
            ),
        ],
        message_plan=MessagePlan(
            template_id="team_weekly_approval",
            template_params={"customer_segment": "dormant", "campaign_mode": "recovery",
                              "projected_recovery_inr": "5000"},
            language=Language.EN,
            personalization="Test personalization.",
        ),
    )


def test_cohort_ids_are_grounded_true_for_real_customers(pool):
    from orchestrator.manager.review import _cohort_ids_are_grounded

    tid, customer_ids = _seed_tenant_with_customers(pool, 3)
    assert _cohort_ids_are_grounded(tid, customer_ids) is True


def test_cohort_ids_are_grounded_false_for_hallucinated_ids(pool):
    from orchestrator.manager.review import _cohort_ids_are_grounded

    tid, customer_ids = _seed_tenant_with_customers(pool, 2)
    hallucinated = [*customer_ids, uuid4()]  # one real, one that never existed
    assert _cohort_ids_are_grounded(tid, hallucinated) is False


def test_cohort_ids_are_grounded_false_for_cross_tenant_ids(pool):
    """A cohort id belonging to a DIFFERENT tenant must not ground — the existence check is
    tenant-scoped (WHERE tenant_id = %s), so it cannot be satisfied by another tenant's customer."""
    from orchestrator.manager.review import _cohort_ids_are_grounded

    _tid_a, customer_ids_a = _seed_tenant_with_customers(pool, 1)
    tid_b, _customer_ids_b = _seed_tenant_with_customers(pool, 1)
    assert _cohort_ids_are_grounded(tid_b, customer_ids_a) is False


def test_cohort_ids_are_grounded_true_for_empty_list(pool):
    from orchestrator.manager.review import _cohort_ids_are_grounded

    tid, _ = _seed_tenant_with_customers(pool, 0)
    assert _cohort_ids_are_grounded(tid, []) is True


def test_adapt_proposed_plan_with_grounded_cohort_completes_and_advances(pool):
    """The full acceptance path: a grounded proposed plan adapts to status='completed' with
    evidence_refs + effect_intents populated, and — through the EXISTING to_legacy bridge +
    decide_next_action — reaches ACCEPT/NEXT_SPECIALIST (never a pushback)."""
    from orchestrator.manager.decision import ManagerDecisionKind, decide_next_action
    from orchestrator.manager.review import (
        adapt_campaign_plan_to_specialist_return,
        to_legacy_specialist_return,
    )

    tid, customer_ids = _seed_tenant_with_customers(pool, 3)
    plan = _proposed_plan(tenant_id=uuid4(), run_id=uuid4(), customer_ids=customer_ids)

    ret = adapt_campaign_plan_to_specialist_return(tid, plan)
    assert ret.status == "completed"
    assert ret.evidence_refs and ret.evidence_refs[0].kind == "campaign_plan"
    assert ret.evidence_refs[0].ref == "test-evidence-1"
    assert ret.effect_intents and ret.effect_intents[0].effect_class == "customer_send"
    assert ret.effect_intents[0].magnitude_minor == 100_000

    legacy = to_legacy_specialist_return(ret)
    decision = decide_next_action(legacy, has_next_step=False)
    assert decision.kind is ManagerDecisionKind.ACCEPT


def test_adapt_proposed_plan_with_ungrounded_cohort_escalates(pool):
    """A hallucinated cohort must NEVER reach ACCEPT — decide_next_action escalates (no proposed
    path), an operator-visible incident rather than a step silently marked done on a plan that
    collapse would have rejected anyway."""
    from orchestrator.manager.decision import ManagerDecisionKind, decide_next_action
    from orchestrator.manager.review import (
        adapt_campaign_plan_to_specialist_return,
        to_legacy_specialist_return,
    )

    tid, _real_ids = _seed_tenant_with_customers(pool, 1)
    hallucinated_ids = [uuid4(), uuid4()]
    plan = _proposed_plan(tenant_id=uuid4(), run_id=uuid4(), customer_ids=hallucinated_ids)

    ret = adapt_campaign_plan_to_specialist_return(tid, plan)
    assert ret.status == "blocked"
    assert ret.reason_code == "ungrounded_cohort"
    assert ret.proposed_outcome is None

    legacy = to_legacy_specialist_return(ret)
    decision = decide_next_action(legacy, has_next_step=False)
    assert decision.kind is ManagerDecisionKind.ESCALATE


def test_adapt_out_of_scope_plan_escalates():
    """OUT_OF_SCOPE: SR genuinely cannot act in-lane — escalate (no proposed path), NOT a silent
    drop or a fabricated plan. Pure — no DB (grounding never runs for a non-proposed variant)."""
    from orchestrator.agent.schemas.campaign_plan import CampaignPlanOutOfScope
    from orchestrator.manager.decision import ManagerDecisionKind, decide_next_action
    from orchestrator.manager.review import (
        adapt_campaign_plan_to_specialist_return,
        to_legacy_specialist_return,
    )

    plan = CampaignPlanOutOfScope(
        tenant_id=uuid4(), run_id=uuid4(), generated_at=datetime.now(UTC),
        out_of_scope_reason="Owner asked about refund policy, not customer recovery.",
    )
    ret = adapt_campaign_plan_to_specialist_return(str(uuid4()), plan)
    assert ret.status == "blocked"
    assert ret.reason_code == "out_of_scope"
    assert ret.proposed_outcome is None

    legacy = to_legacy_specialist_return(ret)
    decision = decide_next_action(legacy, has_next_step=False)
    assert decision.kind is ManagerDecisionKind.ESCALATE


def test_adapt_insufficient_data_plan_revises():
    """INSUFFICIENT_DATA: the Manager CAN reframe or wait — decide_next_action must REVISE (a
    proposed_outcome IS present, derived from the plan's own missing_data), governed by the
    EXISTING per-step revision budget, never an immediate escalate. Pure — no DB."""
    from orchestrator.agent.schemas.campaign_plan import CampaignPlanInsufficientData, MissingDataItem
    from orchestrator.manager.decision import ManagerDecisionKind, decide_next_action
    from orchestrator.manager.review import (
        adapt_campaign_plan_to_specialist_return,
        to_legacy_specialist_return,
    )

    plan = CampaignPlanInsufficientData(
        tenant_id=uuid4(), run_id=uuid4(), generated_at=datetime.now(UTC),
        missing_data=[
            MissingDataItem(
                category="customer_ledger", description="no purchase history on file",
                suggested_remediation="connect a POS/ledger integration",
            ),
        ],
    )
    ret = adapt_campaign_plan_to_specialist_return(str(uuid4()), plan)
    assert ret.status == "blocked"
    assert ret.reason_code == "insufficient_data"
    assert ret.proposed_outcome is not None
    assert "customer_ledger" in ret.proposed_outcome

    legacy = to_legacy_specialist_return(ret)
    decision = decide_next_action(legacy, has_next_step=False)
    assert decision.kind is ManagerDecisionKind.REVISE
