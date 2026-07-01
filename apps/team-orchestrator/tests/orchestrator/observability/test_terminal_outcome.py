"""VT-552 — the terminal-outcome classifier (pure logic, but importing it drags the
observability package __init__ which pulls psycopg → skip under the dep-less smoke)."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")  # observability/__init__ import chain needs psycopg

from orchestrator.observability.terminal_outcome import (  # noqa: E402
    TerminalOutcome,
    classify_terminal,
    is_silent_terminal,
    is_terminal,
)


def test_completed_with_outcome():
    assert classify_terminal(status="completed", final_outcome="sent_winback") is (
        TerminalOutcome.COMPLETED_WITH_OUTCOME
    )


def test_completed_with_effect_is_not_silent():
    assert classify_terminal(status="completed", final_outcome=None, has_effect=True) is (
        TerminalOutcome.COMPLETED_WITH_OUTCOME
    )


def test_completed_no_outcome_no_effect_is_silent():
    assert classify_terminal(status="completed", final_outcome="  ") is (
        TerminalOutcome.COMPLETED_SILENT
    )
    assert is_silent_terminal(status="completed", final_outcome=None) is True


def test_non_completed_terminals():
    assert classify_terminal(status="escalated", final_outcome=None) is TerminalOutcome.ESCALATED
    assert classify_terminal(status="aborted_hard_limit", final_outcome=None) is (
        TerminalOutcome.ABORTED
    )
    assert classify_terminal(status="duplicate_rejected", final_outcome=None) is (
        TerminalOutcome.REJECTED
    )


def test_running_is_not_terminal():
    assert classify_terminal(status="running", final_outcome=None) is TerminalOutcome.RUNNING
    assert is_terminal("running") is False
    assert is_terminal("completed") is True
    assert is_silent_terminal(status="running", final_outcome=None) is False
