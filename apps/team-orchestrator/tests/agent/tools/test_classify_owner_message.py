"""VT-49 — classify_owner_message tests.

Mock Anthropic in CI by default. Real-mode opt-in via
VT49_REAL_API=1 + ANTHROPIC_API_KEY (release-prep manual run only;
NEVER fires in CI per VT-32 hard rule).

7 parametrized scenarios across 5 labels + 2 schema/error paths.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("anthropic")


def _fake_response(*, text: str, input_tokens: int = 1500,
                   output_tokens: int = 80) -> Any:
    class _TextBlock(SimpleNamespace):
        def model_dump(self) -> dict[str, Any]:
            return {"type": "text", "text": self.text}

    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
    )


def _patched_client(response: Any) -> Any:
    fake = MagicMock()
    fake.messages.create.return_value = response
    return fake


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

    fake = _patched_client(_fake_response(text=json.dumps(envelope)))
    result = classify_owner_message(
        ClassifyOwnerMessageInput(text=text, tenant_id="11111111-1111-1111-1111-111111111111"), client=fake,
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
    fake = _patched_client(_fake_response(text="not a json"))
    with pytest.raises(ValueError, match="non-JSON"):
        classify_owner_message(
            ClassifyOwnerMessageInput(text="anything", tenant_id="11111111-1111-1111-1111-111111111111"), client=fake,
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
    fake = _patched_client(_fake_response(text=raw))
    result = classify_owner_message(
        ClassifyOwnerMessageInput(
            text="yes go ahead",
            tenant_id="11111111-1111-1111-1111-111111111111",
        ),
        client=fake,
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
    fake = _patched_client(_fake_response(
        text=json.dumps({"classification": "approval", "confidence": 1.5,
                         "suggested_action": "x"}),
    ))
    with pytest.raises(ValueError, match="envelope validation"):
        classify_owner_message(
            ClassifyOwnerMessageInput(text="anything", tenant_id="11111111-1111-1111-1111-111111111111"), client=fake,
            consent_check=lambda _t: True,
        )


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

    client = MagicMock()  # would record any transmit
    result = classify_owner_message(
        ClassifyOwnerMessageInput(text="please run the diwali campaign", tenant_id="11111111-1111-1111-1111-111111111111"),
        client=client,
        consent_check=lambda _t: False,
    )
    assert result.skipped_reason == "no_owner_inputs_consent"
    assert result.classification == "other"   # → resolve_decision_from_reply maps to None (paused)
    client.messages.create.assert_not_called()  # FAIL-CLOSED: no transmit to the sub-processor


def test_classify_fails_closed_on_consent_check_error() -> None:
    """VT-270: a consent-check error (bad tenant_id / DB hiccup) → fail-closed skip, no transmit."""
    from unittest.mock import MagicMock

    from orchestrator.agent.tools.classify_owner_message import (
        ClassifyOwnerMessageInput,
        classify_owner_message,
    )

    client = MagicMock()

    def _boom(_t):
        raise RuntimeError("db down")

    result = classify_owner_message(
        ClassifyOwnerMessageInput(text="anything", tenant_id="11111111-1111-1111-1111-111111111111"),
        client=client,
        consent_check=_boom,
    )
    assert result.skipped_reason == "consent_check_error"
    client.messages.create.assert_not_called()
