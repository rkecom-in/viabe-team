"""VT-611 (Phase B2, Finding A) — ``manager.shadow_eval``'s DB-backed proofs (live Postgres).

Split out of ``test_shadow_eval.py`` (whose pure/structural tests must NOT skip when
``DATABASE_URL`` is unset — a module-level ``pytestmark`` applies to the WHOLE file regardless of
position, so the DB-only tests need their own module). Covers: the ``business_policy`` spend-
ceiling check (a real, read-only SELECT), the CampaignPlan grounding path (VT-607's REAL
customer-existence query), the actual ``tm_audit_log`` row this pass writes, and a live monkeypatch
proof that none of the mutation-choke functions ``manager.review``'s import graph makes reachable
(but never calls) fire during a real run.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — shadow_eval DB-backed tests skipped",
)

from orchestrator.manager.decision import ManagerDecisionKind  # noqa: E402
from orchestrator.manager.shadow_eval import evaluate_turn_shadow  # noqa: E402


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, json_out: dict) -> None:
        self._json_out = json_out

    def create(self, **kwargs):  # noqa: ANN003, ANN201 — test double
        return _FakeResp([_FakeTextBlock(json.dumps(self._json_out))])


class _FakeClient:
    def __init__(self, json_out: dict) -> None:
        self.messages = _FakeMessages(json_out)


def _payload(**overrides):
    base = {
        "status": "completed",
        "action_summary": "did the thing",
        "outcome_summary": "it worked",
        "evidence_refs": [],
        "effect_intents": [],
        "owner_question": None,
        "proposed_outcome": None,
        "reason_code": None,
    }
    base.update(overrides)
    return base


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


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"se-{tid[:8]}"),
        )
    return tid


def _seed_tenant_with_customers(pool, n: int) -> tuple[str, list]:
    tid = str(uuid4())
    customer_ids: list = []
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"seg-{tid[:8]}"),
        )
        for i in range(n):
            row = conn.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, source) "
                "VALUES (%s, %s, %s, 'subscribed', 'test') RETURNING id",
                (tid, f"Customer {i}", f"+9198{uuid4().int % 10**8:08d}"),
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
            EvidenceRef(claim_id="E1", source_kind=EvidenceSourceKind.TOOL_CALL, source_id="ev-1"),
        ],
        message_plan=MessagePlan(
            template_id="team_weekly_approval",
            template_params={"customer_segment": "dormant", "campaign_mode": "recovery",
                              "projected_recovery_inr": "5000"},
            language=Language.EN,
            personalization="Test personalization.",
        ),
    )


def _tm_audit_row(pool, tenant_id: str, turn_ref: str) -> dict:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT event_layer, event_kind, actor, status, decision, trace_id "
            "FROM tm_audit_log WHERE tenant_id = %s AND event_kind = 'shadow_divergence' "
            "AND trace_id = %s ORDER BY created_at DESC LIMIT 1",
            (tenant_id, turn_ref),
        ).fetchall()
    assert len(rows) == 1, f"expected exactly 1 shadow_divergence row for turn={turn_ref}"
    return rows[0]


def test_campaign_plan_ungrounded_cohort_is_safety_divergence_with_real_audit_row(pool) -> None:
    tid, _real_ids = _seed_tenant_with_customers(pool, 1)
    hallucinated = [uuid4(), uuid4()]
    plan = _proposed_plan(tenant_id=uuid4(), run_id=uuid4(), customer_ids=hallucinated)
    turn_ref = f"SM-ungrounded-{uuid4().hex[:8]}"

    result = evaluate_turn_shadow(
        tid,
        turn_ref=turn_ref,
        situation="s", desired_outcome="d", acceptance_criteria=["c"],
        raw_output="unused when campaign_plan is given",
        campaign_plan=plan,
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ESCALATE
    assert result.divergence_class == "safety_divergence"
    assert result.specialist_return.reason_code == "ungrounded_cohort"
    assert result.audit_id is not None

    row = _tm_audit_row(pool, tid, turn_ref)
    assert row["event_layer"] == "decides"
    assert row["actor"] == "team_manager"
    assert row["status"] == "blocked"
    assert row["decision"]["class"] == "safety_divergence"
    assert row["decision"]["shadow_decision"] == "escalate"
    assert row["decision"]["turn_ref"] == turn_ref


def test_campaign_plan_grounded_proposal_is_no_divergence(pool) -> None:
    tid, customer_ids = _seed_tenant_with_customers(pool, 3)
    plan = _proposed_plan(tenant_id=uuid4(), run_id=uuid4(), customer_ids=customer_ids)
    turn_ref = f"SM-grounded-{uuid4().hex[:8]}"

    result = evaluate_turn_shadow(
        tid,
        turn_ref=turn_ref,
        situation="s", desired_outcome="d", acceptance_criteria=["c"],
        raw_output="unused when campaign_plan is given",
        campaign_plan=plan,
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ACCEPT
    assert result.divergence_class == "no_divergence"
    assert result.out_of_policy_effect_classes == ()


def test_spend_effect_out_of_policy_is_safety_divergence(pool) -> None:
    """A tenant with NO tenant_business_policy row is _DENY_ALL (ceiling=0) — ANY spend magnitude
    is out-of-policy. This is the 'unapproved spend legacy would let through' case: even though
    the specialist's own status is 'completed' (shadow would otherwise ACCEPT), the out-of-policy
    check takes priority."""
    tid = _seed_tenant(pool)
    turn_ref = f"SM-spend-oop-{uuid4().hex[:8]}"

    result = evaluate_turn_shadow(
        tid,
        turn_ref=turn_ref,
        situation="s", desired_outcome="d", acceptance_criteria=["c"],
        raw_output="raw",
        client=_FakeClient(
            _payload(
                status="completed",
                action_summary="paid the ad vendor",
                effect_intents=[
                    {"effect_class": "spend", "summary": "ad spend", "magnitude_minor": 50_000}
                ],
            )
        ),
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ACCEPT  # shadow itself would accept
    assert result.divergence_class == "safety_divergence"  # but the policy bound still catches it
    assert result.out_of_policy_effect_classes == ("spend",)

    row = _tm_audit_row(pool, tid, turn_ref)
    assert row["decision"]["out_of_policy_effect_classes"] == ["spend"]


def test_spend_effect_in_policy_is_no_divergence_when_accepted(pool) -> None:
    from orchestrator.agents.business_policy import grant_business_policy

    tid = _seed_tenant(pool)
    with pool.connection() as conn:
        grant_business_policy(
            tid,
            allowed_action_types=["spend"],
            spend_ceiling_minor=100_000,
            conn=conn,
        )
    turn_ref = f"SM-spend-ok-{uuid4().hex[:8]}"

    result = evaluate_turn_shadow(
        tid,
        turn_ref=turn_ref,
        situation="s", desired_outcome="d", acceptance_criteria=["c"],
        raw_output="raw",
        client=_FakeClient(
            _payload(
                status="completed",
                action_summary="paid the ad vendor",
                effect_intents=[
                    {"effect_class": "spend", "summary": "ad spend", "magnitude_minor": 50_000}
                ],
            )
        ),
    )
    assert result.divergence_class == "no_divergence"
    assert result.out_of_policy_effect_classes == ()


def test_zero_live_effect_mutation_choke_never_fires(pool, monkeypatch) -> None:
    """The load-bearing safety proof: monkeypatch every persistence/decision function
    ``manager.review``'s OWN import graph makes reachable (plan_store/task_store/
    pending_questions/incident_store/decision.record_decision/review.manager_review itself) to
    raise if called, then run a REAL evaluate_turn_shadow (campaign_plan path, hitting the actual
    DB-backed grounding check) end-to-end. Nothing fires."""
    import orchestrator.manager.decision as decision_mod
    import orchestrator.manager.pending_questions as pending_questions_mod
    import orchestrator.manager.plan_store as plan_store_mod
    import orchestrator.manager.review as review_mod
    import orchestrator.manager.task_store as task_store_mod
    import orchestrator.observability.incident_store as incident_store_mod

    def _boom(*args, **kwargs):
        raise AssertionError("shadow_eval must NEVER trigger this mutation/effect path")

    for target, name in (
        (plan_store_mod, "complete_step"),
        (task_store_mod, "set_step_status"),
        (task_store_mod, "set_task_status"),
        (pending_questions_mod, "ask"),
        (incident_store_mod, "create_incident"),
        (incident_store_mod, "escalate_incident"),
        (decision_mod, "record_decision"),
        (review_mod, "manager_review"),
    ):
        monkeypatch.setattr(target, name, _boom)

    tid, _real_ids = _seed_tenant_with_customers(pool, 2)
    hallucinated = [uuid4()]
    plan = _proposed_plan(tenant_id=uuid4(), run_id=uuid4(), customer_ids=hallucinated)

    result = evaluate_turn_shadow(
        tid,
        turn_ref=f"SM-zeroeffect-{uuid4().hex[:8]}",
        situation="s", desired_outcome="d", acceptance_criteria=["c"],
        raw_output="unused",
        campaign_plan=plan,
    )
    # Reaching here at all (no AssertionError raised by _boom) IS the proof.
    assert result.divergence_class == "safety_divergence"
