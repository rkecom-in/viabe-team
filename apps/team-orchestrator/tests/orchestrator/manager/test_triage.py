"""VT-606 (Loop Package 3, execution-plan §3 step 4) — the manager turn-triage classifier.

Fail-soft is the binding contract here: ANY classify failure returns ``None`` (never a guessed
outcome, never a raise) so the caller falls back to the CURRENT dispatch behavior.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("anthropic")

from orchestrator.manager.triage import TriageResult, triage_turn  # noqa: E402


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        return _FakeResp([_FakeTextBlock(self._text)])


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


class _RaisingClient:
    class messages:  # noqa: N801
        @staticmethod
        def create(**kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("network down")


def _json_client(payload: dict) -> _FakeClient:
    return _FakeClient(json.dumps(payload))


def test_new_task_classification() -> None:
    result = triage_turn(
        message_text="win back my lapsed customers",
        has_open_question=False,
        has_active_task=False,
        client=_json_client({"outcome": "new_task", "reasoning": "owner wants a campaign"}),
    )
    assert result == TriageResult(outcome="new_task", reasoning="owner wants a campaign")


def test_direct_reply_classification() -> None:
    result = triage_turn(
        message_text="hi",
        has_open_question=False,
        has_active_task=True,
        client=_json_client({"outcome": "direct_reply", "reasoning": "greeting"}),
    )
    assert result is not None
    assert result.outcome == "direct_reply"


def test_answer_pending_with_open_question() -> None:
    result = triage_turn(
        message_text="yes go ahead",
        has_open_question=True,
        has_active_task=True,
        client=_json_client({"outcome": "answer_pending", "reasoning": "answers the open question"}),
    )
    assert result is not None
    assert result.outcome == "answer_pending"


def test_task_status_classification() -> None:
    result = triage_turn(
        message_text="how's it going?",
        has_open_question=False,
        has_active_task=True,
        client=_json_client({"outcome": "task_status", "reasoning": "asking for status"}),
    )
    assert result is not None
    assert result.outcome == "task_status"


def test_cancel_task_classification() -> None:
    result = triage_turn(
        message_text="stop that campaign",
        has_open_question=False,
        has_active_task=True,
        client=_json_client({"outcome": "cancel_task", "reasoning": "owner wants to stop"}),
    )
    assert result is not None
    assert result.outcome == "cancel_task"


# --- fail-soft: every failure mode returns None, never raises ---------------------------------


def test_fail_soft_on_anthropic_call_exception() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False, client=_RaisingClient()
    ) is None


def test_fail_soft_on_non_json_output() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False,
        client=_FakeClient("not json"),
    ) is None


def test_fail_soft_on_empty_output() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False, client=_FakeClient(""),
    ) is None


def test_fail_soft_on_schema_invalid_output() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False,
        client=_json_client({"outcome": "not_a_real_outcome"}),
    ) is None


def test_fail_soft_on_missing_outcome_field() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False,
        client=_json_client({"reasoning": "no outcome key"}),
    ) is None


# --- deterministic backstop over the LLM's own judgment ---------------------------------------


def test_answer_pending_without_open_question_is_rejected() -> None:
    """The LLM must not be trusted to invent an open question that doesn't exist."""
    assert triage_turn(
        message_text="yes",
        has_open_question=False,
        has_active_task=False,
        client=_json_client({"outcome": "answer_pending", "reasoning": "looks like an answer"}),
    ) is None


def test_strips_code_fence() -> None:
    body = json.dumps({"outcome": "direct_reply", "reasoning": "small talk"})
    result = triage_turn(
        message_text="hey", has_open_question=False, has_active_task=False,
        client=_FakeClient(f"```json\n{body}\n```"),
    )
    assert result is not None
    assert result.outcome == "direct_reply"
