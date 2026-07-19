"""VT-49 — classify_owner_message tests.

Mock Anthropic in CI by default. Real-mode opt-in via
VT49_REAL_API=1 + ANTHROPIC_API_KEY (release-prep manual run only;
NEVER fires in CI per VT-32 hard rule).

7 parametrized scenarios across 5 labels + 2 schema/error paths.
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("pydantic")


def _text_call(raw: str):
    """A ``text_call`` stub returning fixed raw text. Mirrors ``structured_text_call``'s signature
    ``(tier, *, system, user, max_tokens, agent, call_site, tenant_id)`` — accepts and ignores
    whatever the site passes."""

    def _call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        return raw

    return _call


SCENARIOS = [
    pytest.param(
        "yes go ahead",
        {"classification": "approval", "confidence": 0.95,
         "suggested_action": "execute pending plan"},
        "approval",
        id="approval_simple",
    ),
    pytest.param(
        "looks good run it",
        {"classification": "approval", "confidence": 0.9,
         "suggested_action": "execute"},
        "approval",
        id="approval_phrased",
    ),
    pytest.param(
        "no don't do that",
        {"classification": "rejection", "confidence": 0.95,
         "suggested_action": "abort plan"},
        "rejection",
        id="rejection_simple",
    ),
    pytest.param(
        "what would it cost?",
        {"classification": "question", "confidence": 0.9,
         "suggested_action": "answer cost"},
        "question",
        id="question_cost",
    ),
    pytest.param(
        "the timing was off yesterday",
        {"classification": "feedback", "confidence": 0.9,
         "suggested_action": "record feedback"},
        "feedback",
        id="feedback_timing",
    ),
    pytest.param(
        "good morning",
        {"classification": "other", "confidence": 0.85,
         "suggested_action": "ack greeting"},
        "other",
        id="other_greeting",
    ),
    pytest.param(
        "which of my customers have stopped buying?",
        {"classification": "business_analysis", "confidence": 0.9,
         "suggested_action": "analyze lapsed customers via sales recovery"},
        "business_analysis",
        id="business_analysis_lapsed_customers",
    ),
]


@pytest.mark.parametrize("text, envelope, expected_label", SCENARIOS)
def test_classify_owner_message_labels(
    text: str, envelope: dict, expected_label: str,
) -> None:
    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )

    if os.environ.get("VT49_REAL_API") == "1":
        pytest.skip("real-API mode active; mock test skipped")

    result = classify_owner_message(
        ClassifyOwnerMessageInput(text=text, tenant_id="11111111-1111-1111-1111-111111111111"),
        text_call=_text_call(json.dumps(envelope)),
        consent_check=lambda _t: True,  # VT-270: consent on → exercise classification
    )
    assert result.classification == expected_label
    assert 0.0 <= result.confidence <= 1.0


def test_classify_owner_message_invalid_json_raises() -> None:
    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )
    if os.environ.get("VT49_REAL_API") == "1":
        pytest.skip("real-API mode active")
    with pytest.raises(ValueError, match="non-JSON"):
        classify_owner_message(
            ClassifyOwnerMessageInput(text="anything", tenant_id="11111111-1111-1111-1111-111111111111"),
            text_call=_text_call("not a json"),
            consent_check=lambda _t: True,
        )


# --- VT-464 D1: markdown-fenced JSON must parse (the approval-resume crash) ----

_FENCE_CASES = [
    pytest.param(
        '```json\n{"classification": "approval", "confidence": 0.95, '
        '"suggested_action": "execute"}\n```',
        id="fenced_json_label_newlines",
    ),
    pytest.param(
        '```\n{"classification": "approval", "confidence": 0.9, '
        '"suggested_action": "execute"}\n```',
        id="fenced_bare_newlines",
    ),
    pytest.param(
        '```json{"classification": "approval", "confidence": 0.92, '
        '"suggested_action": "execute"}```',
        id="fenced_json_inline",
    ),
    pytest.param(
        '{"classification": "approval", "confidence": 0.91, '
        '"suggested_action": "execute"}',
        id="bare_json_still_parses",
    ),
]


@pytest.mark.parametrize("raw", _FENCE_CASES)
def test_classify_parses_markdown_fenced_json(raw: str) -> None:
    """VT-464 D1: Haiku-4.5 now wraps the JSON envelope in a ```json fence.
    Un-stripped, json.loads raised — and resolve_decision_from_reply
    (runner.py, a direct call with NO try/except) crashed the DBOS step,
    stranding the run 'running' forever. The writer must strip the fence;
    a bare JSON response must still parse unchanged.
    """
    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )
    if os.environ.get("VT49_REAL_API") == "1":
        pytest.skip("real-API mode active")
    result = classify_owner_message(
        ClassifyOwnerMessageInput(
            text="yes go ahead",
            tenant_id="11111111-1111-1111-1111-111111111111",
        ),
        text_call=_text_call(raw),
        consent_check=lambda _t: True,
    )
    assert result.classification == "approval"
    assert 0.0 <= result.confidence <= 1.0


def test_classify_owner_message_invalid_envelope_raises() -> None:
    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )
    if os.environ.get("VT49_REAL_API") == "1":
        pytest.skip("real-API mode active")
    with pytest.raises(ValueError, match="envelope validation"):
        classify_owner_message(
            ClassifyOwnerMessageInput(text="anything", tenant_id="11111111-1111-1111-1111-111111111111"),
            text_call=_text_call(json.dumps({"classification": "approval", "confidence": 1.5,
                                             "suggested_action": "x"})),
            consent_check=lambda _t: True,
        )


# --- VT-595: business_analysis label + v4.0 prompt ---------------------------

def test_business_analysis_in_classification_literal() -> None:
    from orchestrator.agent.tools.classify_owner_message import Classification

    assert "business_analysis" in Classification.__args__


def test_envelope_accepts_business_analysis() -> None:
    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageOutput,
    )

    out = ClassifyOwnerMessageOutput(
        classification="business_analysis",
        confidence=0.9,
        suggested_action="analyze lapsed customers via sales recovery",
    )
    assert out.classification == "business_analysis"


def test_prompt_is_v4_and_carries_business_analysis_and_tightened_status_query() -> None:
    """VT-595: the loaded prompt is v4.0, defines business_analysis, and no longer
    defines status_query broadly enough to swallow a WHICH/WHY analysis question."""
    from orchestrator.agent.tools.classify_owner_message import _SYSTEM_PROMPT

    assert "version=4.0" in _SYSTEM_PROMPT
    assert "business_analysis" in _SYSTEM_PROMPT
    assert "which of my customers have stopped buying" in _SYSTEM_PROMPT.lower()
    # the tightened status_query definition no longer claims to cover an analysis ask
    assert "status_query vs business_analysis" in _SYSTEM_PROMPT


@pytest.mark.skipif(
    os.environ.get("VT49_REAL_API") != "1",
    reason="real-API mode opt-in (VT49_REAL_API=1)",
)
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY required for real-API smoke",
)
def test_classify_owner_message_real_api_smoke() -> None:
    """One real Haiku API call on a clearly-approval input. Asserts
    classification == 'approval'.
    """
    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )
    result = classify_owner_message(
        ClassifyOwnerMessageInput(text="yes looks good run it", tenant_id="11111111-1111-1111-1111-111111111111"),
        consent_check=lambda _t: True,
    )
    assert result.classification == "approval"


# --- VT-270: owner_inputs consent gate (fail-closed, no transmit) -------------

def test_classify_skips_transmit_when_consent_off() -> None:
    """VT-270: owner_inputs OFF → the body is NEVER sent to Anthropic; skipped envelope returned."""
    from unittest.mock import MagicMock

    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )

    transmit = MagicMock()  # the text_call — would record any transmit
    result = classify_owner_message(
        ClassifyOwnerMessageInput(text="please run the diwali campaign", tenant_id="11111111-1111-1111-1111-111111111111"),
        text_call=transmit,
        consent_check=lambda _t: False,
    )
    assert result.skipped_reason == "no_owner_inputs_consent"
    assert result.classification == "other"   # → resolve_decision_from_reply maps to None (paused)
    transmit.assert_not_called()  # FAIL-CLOSED: no transmit to the sub-processor


def test_classify_fails_closed_on_consent_check_error() -> None:
    """VT-270: a consent-check error (bad tenant_id / DB hiccup) → fail-closed skip, no transmit."""
    from unittest.mock import MagicMock

    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )

    transmit = MagicMock()

    def _boom(_t):
        raise RuntimeError("db down")

    result = classify_owner_message(
        ClassifyOwnerMessageInput(text="anything", tenant_id="11111111-1111-1111-1111-111111111111"),
        text_call=transmit,
        consent_check=_boom,
    )
    assert result.skipped_reason == "consent_check_error"
    transmit.assert_not_called()
