"""VT-606 (Loop Package 3) — manager_review's PURE pieces: the review-tier structured-extraction call
(injected transport, no network) + the amendment-A1 legacy adapter + the decision-outcome mapping. No
DB required — the DB-backed ``manager_review()`` end-to-end effects are in
``test_manager_review_db.py``.

VT-732 — the extraction call goes through the multi-provider seam, so the injection point is
``text_call`` (a callable returning the raw text) rather than an Anthropic SDK client double. Same
contract under test: whatever text comes back is fence-stripped, JSON-parsed, and schema-validated,
and a failure raises ``ValueError`` for the caller's fail-closed path.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from orchestrator.manager.plan_models import EffectIntent, EvidenceRef, PlanSpecialistReturn  # noqa: E402
from orchestrator.manager.review import (  # noqa: E402
    _DECISION_TO_OUTCOME,
    extract_specialist_return,
    to_legacy_specialist_return,
)


def _text_call(text: str):
    """A transport double: returns ``text`` verbatim and pins the tier the caller asked for."""

    def _call(tier: str, **kwargs):  # noqa: ANN003, ANN202 — test double
        assert tier == "complex", "review runs on the env-governed complex tier"
        return text

    return _call


def _json_call(json_out: dict):
    return _text_call(json.dumps(json_out))


_BASE_KWARGS = {
    "situation": "60d dormant cohort",
    "desired_outcome": "re-engage",
    "acceptance_criteria": ["3+ recovered"],
    "raw_output": "campaign_plan proposed for 40 customers",
}


def test_extract_specialist_return_completed() -> None:
    ret = extract_specialist_return(
        **_BASE_KWARGS,
        text_call=_json_call(
            {
                "status": "completed",
                "action_summary": "proposed campaign",
                "outcome_summary": "40 customers targeted",
                "evidence_refs": [{"kind": "campaign_plan", "ref": "cp-1"}],
                "effect_intents": [],
                "owner_question": None,
                "proposed_outcome": None,
                "reason_code": None,
            }
        ),
    )
    assert ret.status == "completed"
    assert ret.evidence_refs == [EvidenceRef(kind="campaign_plan", ref="cp-1")]


def test_extract_specialist_return_non_json_raises() -> None:
    with pytest.raises(ValueError, match="non-JSON"):
        extract_specialist_return(**_BASE_KWARGS, text_call=_text_call("not json at all"))


def test_extract_specialist_return_empty_raises() -> None:
    """An empty response is an extraction FAILURE, not an empty result — the caller fails closed."""
    with pytest.raises(ValueError, match="empty"):
        extract_specialist_return(**_BASE_KWARGS, text_call=_text_call("   "))


def test_extract_specialist_return_schema_invalid_raises() -> None:
    with pytest.raises(ValueError, match="validation failed"):
        extract_specialist_return(
            **_BASE_KWARGS,
            text_call=_json_call({"status": "not_a_real_status"}),
        )


def test_extract_specialist_return_strips_code_fence() -> None:
    body = json.dumps(
        {
            "status": "failed",
            "action_summary": "",
            "outcome_summary": "no consent",
            "reason_code": "no_consent",
        }
    )
    ret = extract_specialist_return(**_BASE_KWARGS, text_call=_text_call(f"```json\n{body}\n```"))
    assert ret.status == "failed"
    assert ret.reason_code == "no_consent"


# --- amendment A1 adapter -------------------------------------------------------------------


def test_adapter_completed_maps_to_action_taken() -> None:
    ret = PlanSpecialistReturn(
        status="completed", action_summary="sent winback", outcome_summary="3 re-engaged"
    )
    legacy = to_legacy_specialist_return(ret)
    assert legacy.pushback is False
    assert legacy.action_taken == "sent winback"
    assert legacy.outcome == "3 re-engaged"


def test_adapter_needs_owner_input_maps_to_no_action() -> None:
    ret = PlanSpecialistReturn(status="needs_owner_input", owner_question="which cohort?")
    legacy = to_legacy_specialist_return(ret)
    assert legacy.pushback is False
    assert legacy.action_taken == ""


def test_adapter_blocked_with_proposed_outcome_maps_to_pushback_with_path() -> None:
    ret = PlanSpecialistReturn(
        status="blocked", reason_code="no_consent", proposed_outcome="wait for consent"
    )
    legacy = to_legacy_specialist_return(ret)
    assert legacy.pushback is True
    assert legacy.proposed_outcome == "wait for consent"


def test_adapter_blocked_with_no_proposed_outcome_maps_to_pushback_no_path() -> None:
    ret = PlanSpecialistReturn(status="blocked", reason_code="no_consent")
    legacy = to_legacy_specialist_return(ret)
    assert legacy.pushback is True
    assert legacy.proposed_outcome == ""


def test_adapter_failed_maps_to_pushback() -> None:
    ret = PlanSpecialistReturn(status="failed", reason_code="tool_error")
    legacy = to_legacy_specialist_return(ret)
    assert legacy.pushback is True


# --- outcome mapping table -------------------------------------------------------------------


def test_decision_to_outcome_covers_all_five_decision_kinds() -> None:
    from orchestrator.manager.decision import ManagerDecisionKind

    assert set(_DECISION_TO_OUTCOME) == set(ManagerDecisionKind)
    assert _DECISION_TO_OUTCOME[ManagerDecisionKind.ACCEPT] == "complete"
    assert _DECISION_TO_OUTCOME[ManagerDecisionKind.NEXT_SPECIALIST] == "continue"
    assert _DECISION_TO_OUTCOME[ManagerDecisionKind.REVISE] == "revise_step"
    assert _DECISION_TO_OUTCOME[ManagerDecisionKind.CLARIFY] == "ask_owner"
    assert _DECISION_TO_OUTCOME[ManagerDecisionKind.ESCALATE] == "escalate"


def test_effect_intent_is_a_proposal_never_executable() -> None:
    """Structural: EffectIntent carries only data fields — no send/execute/commit method."""
    intent = EffectIntent(effect_class="customer_send", summary="send a reminder")
    for forbidden in ("send", "execute", "commit", "spend"):
        assert not hasattr(intent, forbidden)
