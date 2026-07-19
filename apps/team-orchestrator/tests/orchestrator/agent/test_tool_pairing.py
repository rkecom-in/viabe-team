"""VT-622 — repair_orphaned_tool_use: a dual-spawn orphans a lane's tool_use → 400.

The healthy path MUST be identity (no-op) so wiring this into every lane's model call can
never degrade a valid turn; an orphaned tool_use MUST be repaired into a valid pairing.
"""
from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langchain")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from orchestrator.agent.tool_pairing import repair_orphaned_tool_use  # noqa: E402


def _ai_with_calls(calls: list[tuple[str, str]]) -> AIMessage:
    """AIMessage carrying tool_calls [(id, name), ...]."""
    return AIMessage(
        content="",
        tool_calls=[{"id": cid, "name": name, "args": {}, "type": "tool_call"} for cid, name in calls],
    )


def _ids(messages: list) -> set[str]:
    return {
        m.tool_call_id
        for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }


def test_valid_conversation_is_identity_noop():
    # Every tool_use paired → returns the SAME list object (strict no-op).
    msgs = [
        HumanMessage(content="hi"),
        _ai_with_calls([("A", "spawn_x")]),
        ToolMessage(content="ok", tool_call_id="A", name="spawn_x"),
    ]
    assert repair_orphaned_tool_use(msgs) is msgs


def test_no_tool_calls_is_identity_noop():
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
    assert repair_orphaned_tool_use(msgs) is msgs


def test_dual_spawn_orphan_is_repaired():
    # Orchestrator emits TWO tool_use in one turn; only the first got a result.
    msgs = [
        HumanMessage(content="connect sheets and winback"),
        _ai_with_calls([("A", "spawn_sales_recovery"), ("B", "spawn_integration")]),
        ToolMessage(content="handoff", tool_call_id="A", name="spawn_sales_recovery"),
    ]
    fixed = repair_orphaned_tool_use(msgs)
    assert fixed is not msgs  # changed
    # both ids now resolved
    assert _ids(fixed) == {"A", "B"}
    # synthetic result for B is inserted immediately after the AIMessage (index 1),
    # i.e. before the real result for A — all results consecutive after the assistant turn.
    ai_idx = next(i for i, m in enumerate(fixed) if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(fixed[ai_idx + 1], ToolMessage)
    synthetic = next(m for m in fixed if isinstance(m, ToolMessage) and m.tool_call_id == "B")
    assert synthetic.status == "error"


def test_multiple_orphans_across_turns_all_repaired():
    msgs = [
        _ai_with_calls([("A", "t1")]),          # orphan
        _ai_with_calls([("B", "t2"), ("C", "t3")]),  # C orphan; B resolved below
        ToolMessage(content="ok", tool_call_id="B", name="t2"),
    ]
    fixed = repair_orphaned_tool_use(msgs)
    assert _ids(fixed) == {"A", "B", "C"}


def test_all_orphans_when_no_results_at_all():
    msgs = [_ai_with_calls([("A", "t1"), ("B", "t2")])]
    fixed = repair_orphaned_tool_use(msgs)
    assert _ids(fixed) == {"A", "B"}
    # 1 AI + 2 synthetic results
    assert len(fixed) == 3
