"""VT-606 (Loop Package 3) — manager_review's PURE pieces: the sonnet-5 structured-extraction call
(mocked client, no network) + the amendment-A1 legacy adapter + the decision-outcome mapping. No
DB required — the DB-backed ``manager_review()`` end-to-end effects are in
``test_manager_review_db.py``.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("anthropic")

from orchestrator.manager.plan_models import EffectIntent, EvidenceRef, PlanSpecialistReturn  # noqa: E402
from orchestrator.manager.review import (  # noqa: E402
    _DECISION_TO_OUTCOME,
    extract_specialist_return,
    to_legacy_specialist_return,
)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, json_out: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._json_out = json_out
        self._raise_exc = raise_exc

    def create(self, **kwargs):  # noqa: ANN003, ANN201 — test double
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResp([_FakeTextBlock(json.dumps(self._json_out))])


class _FakeClient:
    def __init__(self, json_out: dict | None = None, raise_exc: Exception | None = None) -> None:
        self.messages = _FakeMessages(json_out, raise_exc)


_BASE_KWARGS = {
    "situation": "60d dormant cohort",
    "desired_outcome": "re-engage",
    "acceptance_criteria": ["3+ recovered"],
    "raw_output": "campaign_plan proposed for 40 customers",
}


def test_extract_specialist_return_completed() -> None:
    ret = extract_specialist_return(
        **_BASE_KWARGS,
        client=_FakeClient(
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
    class _RawTextClient:
        class messages:  # noqa: N801 — test double
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock("not json at all")])

    with pytest.raises(ValueError, match="non-JSON"):
        extract_specialist_return(**_BASE_KWARGS, client=_RawTextClient())


def test_extract_specialist_return_schema_invalid_raises() -> None:
    with pytest.raises(ValueError, match="validation failed"):
        extract_specialist_return(
            **_BASE_KWARGS,
            client=_FakeClient({"status": "not_a_real_status"}),
        )


def test_extract_specialist_return_strips_code_fence() -> None:
    class _FencedClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                body = json.dumps(
                    {
                        "status": "failed",
                        "action_summary": "",
                        "outcome_summary": "no consent",
                        "reason_code": "no_consent",
                    }
                )
                return _FakeResp([_FakeTextBlock(f"```json\n{body}\n```")])

    ret = extract_specialist_return(**_BASE_KWARGS, client=_FencedClient())
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
