"""State-machine tests for core.pick_next_action.

Each case is the verbatim policy from the VT-AGENTSDK-LOOP brief. Pure-function
tests — no I/O, no SDK calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))

from core import ProcessSignal, StartTask, pick_next_action  # noqa: E402


def _sig(path: str, task: str | None, sig_type: str = "brief-ready") -> dict:
    return {"path": Path(path), "task": task, "type": sig_type}


def test_all_done_returns_none() -> None:
    state = {"VT-A": "done", "VT-B": "done"}
    assert pick_next_action(state, []) is None


def test_one_queued_no_active_no_signals_dispatches_queued() -> None:
    state = {"VT-A": "queued"}
    assert pick_next_action(state, []) == StartTask(task_id="VT-A")


def test_implementing_with_self_signal_returns_signal() -> None:
    state = {"VT-A": "implementing"}
    inbox = [_sig("/inbox/sig1.md", "VT-A", "review")]
    result = pick_next_action(state, inbox)
    assert isinstance(result, ProcessSignal)
    assert result.task == "VT-A"
    assert result.path == Path("/inbox/sig1.md")
    assert result.sig_type == "review"


def test_implementing_with_other_task_signal_returns_none() -> None:
    state = {"VT-A": "implementing", "VT-B": "queued"}
    inbox = [_sig("/inbox/sig1.md", "VT-B", "brief-ready")]
    assert pick_next_action(state, inbox) is None


def test_in_pr_with_self_signal_returns_signal() -> None:
    state = {"VT-A": "in-pr", "VT-B": "queued"}
    inbox = [_sig("/inbox/sig1.md", "VT-A", "task")]
    result = pick_next_action(state, inbox)
    assert isinstance(result, ProcessSignal)
    assert result.task == "VT-A"


def test_blocked_skipped_queued_dispatched() -> None:
    state = {"VT-A": "blocked", "VT-B": "queued"}
    assert pick_next_action(state, []) == StartTask(task_id="VT-B")


def test_deferred_skipped_queued_dispatched() -> None:
    state = {"VT-A": "deferred", "VT-B": "queued"}
    assert pick_next_action(state, []) == StartTask(task_id="VT-B")


def test_planning_task_is_also_busy_state() -> None:
    state = {"VT-A": "planning", "VT-B": "queued"}
    inbox = [_sig("/inbox/sig1.md", "VT-B", "brief-ready")]
    assert pick_next_action(state, inbox) is None


def test_idle_state_oldest_signal_dispatched_first() -> None:
    state = {"VT-A": "in-pr", "VT-B": "queued"}
    inbox = [
        _sig("/inbox/old.md", "VT-A", "task"),
        _sig("/inbox/new.md", "VT-B", "brief-ready"),
    ]
    result = pick_next_action(state, inbox)
    assert isinstance(result, ProcessSignal)
    assert result.path == Path("/inbox/old.md")
