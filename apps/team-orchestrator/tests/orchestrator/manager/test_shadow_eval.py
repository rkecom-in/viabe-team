"""VT-611 (Phase B2, Finding A) — ``manager.shadow_eval``'s observational pass, PURE/STRUCTURAL
half. No DB required — the divergence classification (safety vs intent vs none) over a
mocked-client extraction, plus a hard structural + dynamic proof the module never even reaches the
send/spawn/mutation surface it must never touch.

The DB-backed half (business_policy spend-ceiling check, CampaignPlan grounding, the real
``tm_audit_log`` row, the live mutation-choke monkeypatch proof) is ``test_shadow_eval_db.py`` — a
SEPARATE module because a ``pytestmark`` skipif applies to the WHOLE file regardless of position,
and these pure tests must never skip just because ``DATABASE_URL`` is unset.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("anthropic")

from orchestrator.manager.decision import ManagerDecisionKind  # noqa: E402
from orchestrator.manager.shadow_eval import evaluate_turn_shadow  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Anthropic client — same shape as test_review_extraction.py's double.
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, json_out: dict | None = None, raw_text: str | None = None) -> None:
        self._json_out = json_out
        self._raw_text = raw_text

    def create(self, **kwargs):  # noqa: ANN003, ANN201 — test double
        if self._raw_text is not None:
            return _FakeResp([_FakeTextBlock(self._raw_text)])
        return _FakeResp([_FakeTextBlock(json.dumps(self._json_out))])


class _FakeClient:
    def __init__(self, json_out: dict | None = None, raw_text: str | None = None) -> None:
        self.messages = _FakeMessages(json_out, raw_text)


_BASE_KWARGS = {
    "turn_ref": "SMtest0000000000000000000000000000",
    "situation": "60d dormant cohort",
    "desired_outcome": "re-engage",
    "acceptance_criteria": ["3+ recovered"],
    "raw_output": "the specialist's raw terminal output",
}


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


# ---------------------------------------------------------------------------
# 1. PURE divergence classification (no DB — no "spend" effect class in any of these, so
#    _out_of_policy_effect_classes never opens a connection).
# ---------------------------------------------------------------------------


def test_no_divergence_when_shadow_accepts_and_legacy_landed() -> None:
    result = evaluate_turn_shadow(
        str(uuid4()),
        **_BASE_KWARGS,
        client=_FakeClient(_payload(status="completed", action_summary="sent winback")),
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ACCEPT
    assert result.divergence_class == "no_divergence"
    assert result.out_of_policy_effect_classes == ()


def test_intent_divergence_when_shadow_disagrees_with_no_effect_at_stake() -> None:
    """needs_owner_input -> CLARIFY: a routing disagreement, but nothing consequential proposed."""
    result = evaluate_turn_shadow(
        str(uuid4()),
        **_BASE_KWARGS,
        client=_FakeClient(
            _payload(status="needs_owner_input", action_summary="", owner_question="which cohort?")
        ),
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.CLARIFY
    assert result.divergence_class == "intent_divergence"


def test_safety_divergence_when_consequential_effect_and_shadow_would_escalate() -> None:
    """blocked + a customer_send effect intent + legacy actually completed this turn -> the new
    loop would ESCALATE something legacy let stand unreviewed."""
    result = evaluate_turn_shadow(
        str(uuid4()),
        **_BASE_KWARGS,
        legacy_final_status="completed",
        client=_FakeClient(
            _payload(
                status="blocked",
                action_summary="",
                reason_code="no_consent",
                effect_intents=[
                    {
                        "effect_class": "customer_send",
                        "summary": "would send a reminder",
                        "magnitude_minor": None,
                    }
                ],
            )
        ),
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ESCALATE
    assert result.divergence_class == "safety_divergence"


def test_intent_divergence_capped_when_legacy_did_not_land() -> None:
    """Identical raw material to the safety case above, but legacy itself never completed this
    turn (it escalated first) — no real customer-facing effect occurred, so the ceiling is
    intent_divergence, never safety."""
    result = evaluate_turn_shadow(
        str(uuid4()),
        **_BASE_KWARGS,
        legacy_final_status="escalated",
        client=_FakeClient(
            _payload(
                status="blocked",
                action_summary="",
                reason_code="no_consent",
                effect_intents=[
                    {
                        "effect_class": "customer_send",
                        "summary": "would send a reminder",
                        "magnitude_minor": None,
                    }
                ],
            )
        ),
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ESCALATE
    assert result.divergence_class == "intent_divergence"


def test_extraction_failure_fails_closed_without_manufacturing_a_safety_alarm() -> None:
    """A non-JSON model reply — ``extract_specialist_return``'s own ``ValueError`` contract (mirrors
    ``test_review_extraction.py::test_extract_specialist_return_non_json_raises``) — fails closed
    to a 'failed' specialist return — ESCALATE, same as manager_review's own contract — but with NO
    effect_intents known, so it lands as intent_divergence, not a false safety alarm over an LLM
    formatting blip."""
    result = evaluate_turn_shadow(
        str(uuid4()),
        **_BASE_KWARGS,
        client=_FakeClient(raw_text="not json at all"),
    )
    assert result.shadow_decision_kind is ManagerDecisionKind.ESCALATE
    assert result.divergence_class == "intent_divergence"
    assert result.specialist_return.reason_code == "extraction_failed"


# ---------------------------------------------------------------------------
# 2. Structural proof — shadow_eval.py's OWN source never references the send/spawn/mutation
#    surface. Grepped, not merely trusted (mirrors the #6 hardening's own discipline).
# ---------------------------------------------------------------------------


def test_module_source_never_references_send_spawn_or_mutation_symbols() -> None:
    """AST-based (not a naive substring grep — the module's OWN docstring names these exact
    functions to EXPLAIN why it never calls them, which would false-positive a plain ``in src``
    check). Collects every ``Name``/``Attribute`` identifier actually used in CODE — docstrings and
    comments are string constants, invisible to this walk — and asserts none is a forbidden
    mutation/effect symbol."""
    import ast
    import inspect

    from orchestrator.manager import shadow_eval as mod

    tree = ast.parse(inspect.getsource(mod))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    forbidden = {
        "spawn_integration", "spawn_sales_recovery", "spawn_onboarding_conductor",
        "send_template_message", "send_freeform_message", "send_interactive_message",
        "complete_step", "set_step_status", "set_task_status",
        "create_incident", "escalate_incident",
        "manager_review", "record_decision", "grant_business_policy",
    }
    hits = identifiers & forbidden
    assert not hits, f"shadow_eval.py CODE references forbidden mutation/effect symbols: {hits}"


def test_calling_evaluate_turn_shadow_never_imports_twilio_send() -> None:
    """Dynamic (not just structural): a real call must never pull ``orchestrator.utils.
    twilio_send`` into ``sys.modules`` for the first time (the send layer is unreachable from
    every function this module calls)."""
    import sys

    before = set(sys.modules)
    evaluate_turn_shadow(
        str(uuid4()),
        **_BASE_KWARGS,
        client=_FakeClient(_payload(status="completed")),
    )
    newly_imported = set(sys.modules) - before
    assert "orchestrator.utils.twilio_send" not in newly_imported
