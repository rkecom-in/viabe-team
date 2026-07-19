"""VT-605 (Loop Package 2, execution-plan §2) — ManagerPlan / PlanStep / PlanSpecialistReturn
model validation. Pure pydantic — no DB, no langgraph; dep-less-smoke-safe.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError  # noqa: E402

from orchestrator.manager.plan_models import (  # noqa: E402
    EFFECT_CLASSES,
    EffectIntent,
    EvidenceRef,
    ManagerPlan,
    PlanSpecialistReturn,
    PlanStep,
)


def _step(**overrides):
    fields = {"step_seq": 1, "kind": "effect"}
    fields.update(overrides)
    return PlanStep(**fields)


# --- PlanStep ------------------------------------------------------------------------------------


def test_specialist_dispatch_requires_specialist():
    with pytest.raises(ValidationError):
        _step(kind="specialist_dispatch")


def test_specialist_only_valid_on_dispatch_kind():
    with pytest.raises(ValidationError):
        _step(kind="effect", specialist="sales_recovery_agent")


def test_specialist_dispatch_with_specialist_constructs():
    step = _step(kind="specialist_dispatch", specialist="onboarding_conductor")
    assert step.specialist == "onboarding_conductor"


@pytest.mark.parametrize("bogus", ["shopify", "marketing_lane", "sales_recovery"])
def test_unknown_specialist_rejected(bogus):
    with pytest.raises(ValidationError):
        _step(kind="specialist_dispatch", specialist=bogus)


def test_unknown_step_kind_rejected():
    with pytest.raises(ValidationError):
        _step(kind="bogus_kind")


def test_advisory_tool_step_cannot_declare_effects():
    with pytest.raises(ValidationError):
        _step(kind="advisory_tool", allowed_effect_classes=["spend"])


def test_advisory_tool_step_with_no_effects_constructs():
    step = _step(kind="advisory_tool")
    assert step.allowed_effect_classes == []


@pytest.mark.parametrize("bogus", ["bogus_effect", "send_customer", ""])
def test_unknown_effect_class_rejected(bogus):
    with pytest.raises(ValidationError):
        _step(kind="effect", allowed_effect_classes=[bogus])


def test_known_effect_classes_all_accepted():
    step = _step(kind="effect", allowed_effect_classes=sorted(EFFECT_CLASSES))
    assert set(step.allowed_effect_classes) == EFFECT_CLASSES


def test_effect_classes_match_business_policy_action_class():
    """No parallel vocabulary — reuses business_policy.PolicyActionClass byte-for-byte."""
    from orchestrator.agents.business_policy import PolicyActionClass

    assert EFFECT_CLASSES == {c.value for c in PolicyActionClass}


# --- ManagerPlan ----------------------------------------------------------------------------------


def test_steps_must_be_sequential_and_numbered_from_one():
    with pytest.raises(ValidationError):
        ManagerPlan(objective="x", steps=[_step(step_seq=2), _step(step_seq=3)])


def test_steps_must_be_unique():
    with pytest.raises(ValidationError):
        ManagerPlan(objective="x", steps=[_step(step_seq=1), _step(step_seq=1)])


def test_out_of_order_step_seq_still_rejected_even_if_set_covers_1_to_n():
    """Sequential means IN ORDER, not just 'the right set of numbers'."""
    with pytest.raises(ValidationError):
        ManagerPlan(objective="x", steps=[_step(step_seq=2), _step(step_seq=1)])


def test_valid_sequential_plan_constructs():
    plan = ManagerPlan(objective="x", steps=[_step(step_seq=1), _step(step_seq=2), _step(step_seq=3)])
    assert [s.step_seq for s in plan.steps] == [1, 2, 3]
    assert plan.plan_revision == 1
    assert plan.schema_version == "1"


def test_max_eight_steps_enforced():
    with pytest.raises(ValidationError):
        ManagerPlan(objective="x", steps=[_step(step_seq=i) for i in range(1, 10)])


def test_eight_steps_is_the_boundary_and_is_accepted():
    plan = ManagerPlan(objective="x", steps=[_step(step_seq=i) for i in range(1, 9)])
    assert len(plan.steps) == 8


def test_at_least_one_step_required():
    with pytest.raises(ValidationError):
        ManagerPlan(objective="x", steps=[])


def test_extra_field_rejected_fail_closed():
    with pytest.raises(ValidationError):
        ManagerPlan(objective="x", steps=[_step(step_seq=1)], not_a_real_field="oops")


# --- EvidenceRef / EffectIntent -------------------------------------------------------------------


def test_evidence_ref_kind_fail_closed():
    with pytest.raises(ValidationError):
        EvidenceRef(kind="bogus_kind", ref="x")


def test_evidence_ref_matches_task_store_evidence_kinds():
    """Every EvidenceRef.kind must be a real task_store evidence_kind (mig 165 superset)."""
    from orchestrator.manager import task_store

    for kind in ("campaign_plan", "agent_work_item", "pipeline_run", "pipeline_step"):
        assert kind in task_store.EVIDENCE_KINDS
        EvidenceRef(kind=kind, ref="x")  # constructs without raising


def test_effect_intent_unknown_effect_class_rejected():
    with pytest.raises(ValidationError):
        EffectIntent(effect_class="bogus", summary="x")


def test_effect_intent_negative_magnitude_rejected():
    with pytest.raises(ValidationError):
        EffectIntent(effect_class="spend", summary="x", magnitude_minor=-1)


# --- PlanSpecialistReturn --------------------------------------------------------------------------


def test_needs_owner_input_requires_owner_question():
    with pytest.raises(ValidationError):
        PlanSpecialistReturn(status="needs_owner_input")


def test_blocked_requires_reason_code():
    with pytest.raises(ValidationError):
        PlanSpecialistReturn(status="blocked")


def test_completed_needs_neither():
    ret = PlanSpecialistReturn(status="completed", action_summary="did x", outcome_summary="y")
    assert ret.owner_question is None
    assert ret.reason_code is None


def test_needs_owner_input_with_question_constructs():
    ret = PlanSpecialistReturn(status="needs_owner_input", owner_question="which cohort?")
    assert ret.owner_question == "which cohort?"


def test_blocked_with_reason_code_constructs():
    ret = PlanSpecialistReturn(status="blocked", reason_code="no_consent")
    assert ret.reason_code == "no_consent"


def test_effect_intents_are_proposals_never_a_direct_action():
    """Structural: PlanSpecialistReturn carries only DATA fields — no execute/send/commit method
    or callable on the type; effect_intents is a plain list of EffectIntent proposals."""
    ret = PlanSpecialistReturn(
        status="completed", action_summary="x", outcome_summary="y",
        effect_intents=[EffectIntent(effect_class="customer_send", summary="send a reminder")],
    )
    assert ret.effect_intents[0].effect_class == "customer_send"
    # No send/execute/commit callable anywhere on the model.
    forbidden = ("send", "execute", "commit", "spend")
    assert not any(hasattr(ret, name) for name in forbidden)


# --- Amendment A1 regression: this is NOT roster.SpecialistReturn, and does not replace it --------


def test_plan_specialist_return_is_distinct_from_legacy_roster_specialist_return():
    """CC amendment A1 (manager-loop-program.md): the new §2 return shape must NOT replace the
    LIVE ``roster.SpecialistReturn`` dataclass the specialist_return.py bridge consumes today.
    This is a REGRESSION LOCK — if a future change makes these the same object, the legacy
    tagged-union CampaignPlan -> collapse -> VT-594 owner-surfacing path is no longer guaranteed
    byte-compatible, which A1 explicitly forbids pre-shadow."""
    pytest.importorskip("langgraph")
    pytest.importorskip("langchain_anthropic")
    from orchestrator.agent.roster import SpecialistReturn as LegacySpecialistReturn

    assert PlanSpecialistReturn is not LegacySpecialistReturn
    # The legacy dataclass's 5-field shape (pushback/action_taken/outcome/proposed_outcome/reason)
    # still constructs unchanged — VT-605 touched NOTHING on that type.
    legacy = LegacySpecialistReturn(action_taken="sent winback", outcome="3 re-engaged")
    assert legacy.pushback is False
    assert legacy.proposed_outcome == ""


def test_specialist_return_bridge_still_constructs_the_legacy_type_unaffected():
    """agent/specialist_return.py's parse_specialist_return builds roster.SpecialistReturn — prove
    it still does, untouched by the new plan_models module existing alongside it."""
    pytest.importorskip("langgraph")
    pytest.importorskip("langchain_anthropic")
    from orchestrator.agent.specialist_return import parse_specialist_return

    ret = parse_specialist_return({"pushback": True, "reason": "no consent", "proposed_outcome": "wait"})
    assert ret is not None
    assert ret.pushback is True
    assert type(ret).__module__ == "orchestrator.agent.roster"
