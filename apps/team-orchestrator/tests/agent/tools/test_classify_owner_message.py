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
        ClassifyOwnerMessageInput(text=text), client=fake,
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
            ClassifyOwnerMessageInput(text="anything"), client=fake,
        )


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
            ClassifyOwnerMessageInput(text="anything"), client=fake,
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
        ClassifyOwnerMessageInput(text="yes looks good run it"),
    )
    assert result.classification == "approval"
