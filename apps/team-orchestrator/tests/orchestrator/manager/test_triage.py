"""VT-606 (Loop Package 3, execution-plan §3 step 4) — the manager turn-triage classifier.

Fail-soft is the binding contract here: ANY classify failure returns ``None`` (never a guessed
outcome, never a raise) so the caller falls back to the CURRENT dispatch behavior.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from orchestrator.manager.triage import TriageResult, triage_turn  # noqa: E402


def _text_call(raw: str):
    """A ``text_call`` stub returning fixed raw text. Mirrors ``structured_text_call``'s signature
    ``(tier, *, system, user, max_tokens, agent, call_site, tenant_id)`` — it accepts and ignores
    whatever the site passes."""

    def _call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        return raw

    return _call


def _json_call(payload: dict):
    return _text_call(json.dumps(payload))


def _raising_call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
    raise RuntimeError("network down")


def test_new_task_classification() -> None:
    result = triage_turn(
        message_text="win back my lapsed customers",
        has_open_question=False,
        has_active_task=False,
        text_call=_json_call({"outcome": "new_task", "reasoning": "owner wants a campaign"}),
    )
    assert result == TriageResult(outcome="new_task", reasoning="owner wants a campaign")


def test_direct_reply_classification() -> None:
    result = triage_turn(
        message_text="hi",
        has_open_question=False,
        has_active_task=True,
        text_call=_json_call({"outcome": "direct_reply", "reasoning": "greeting"}),
    )
    assert result is not None
    assert result.outcome == "direct_reply"


def test_answer_pending_with_open_question() -> None:
    result = triage_turn(
        message_text="yes go ahead",
        has_open_question=True,
        has_active_task=True,
        text_call=_json_call({"outcome": "answer_pending", "reasoning": "answers the open question"}),
    )
    assert result is not None
    assert result.outcome == "answer_pending"


def test_task_status_classification() -> None:
    result = triage_turn(
        message_text="how's it going?",
        has_open_question=False,
        has_active_task=True,
        text_call=_json_call({"outcome": "task_status", "reasoning": "asking for status"}),
    )
    assert result is not None
    assert result.outcome == "task_status"


def test_cancel_task_classification() -> None:
    result = triage_turn(
        message_text="stop that campaign",
        has_open_question=False,
        has_active_task=True,
        text_call=_json_call({"outcome": "cancel_task", "reasoning": "owner wants to stop"}),
    )
    assert result is not None
    assert result.outcome == "cancel_task"


# --- fail-soft: every failure mode returns None, never raises ---------------------------------


def test_fail_soft_on_anthropic_call_exception() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False, text_call=_raising_call
    ) is None


def test_fail_soft_on_non_json_output() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False,
        text_call=_text_call("not json"),
    ) is None


def test_fail_soft_on_empty_output() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False, text_call=_text_call(""),
    ) is None


def test_fail_soft_on_schema_invalid_output() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False,
        text_call=_json_call({"outcome": "not_a_real_outcome"}),
    ) is None


def test_fail_soft_on_missing_outcome_field() -> None:
    assert triage_turn(
        message_text="x", has_open_question=False, has_active_task=False,
        text_call=_json_call({"reasoning": "no outcome key"}),
    ) is None


# --- deterministic backstop over the LLM's own judgment ---------------------------------------


def test_answer_pending_without_open_question_is_rejected() -> None:
    """The LLM must not be trusted to invent an open question that doesn't exist."""
    assert triage_turn(
        message_text="yes",
        has_open_question=False,
        has_active_task=False,
        text_call=_json_call({"outcome": "answer_pending", "reasoning": "looks like an answer"}),
    ) is None


def test_strips_code_fence() -> None:
    body = json.dumps({"outcome": "direct_reply", "reasoning": "small talk"})
    result = triage_turn(
        message_text="hey", has_open_question=False, has_active_task=False,
        text_call=_text_call(f"```json\n{body}\n```"),
    )
    assert result is not None
    assert result.outcome == "direct_reply"
