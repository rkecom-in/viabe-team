"""VT-606 amendment A4 — "DBOS step-retry mid-graph (re-invoke with same task after a simulated
failure — no duplicate system messages, the VT-602 control-test pattern)."

Mirrors ``tests/orchestrator/agent/test_dispatch_classify.py``'s
``test_vt602_retry_without_stable_ids_reproduces_the_reported_crash`` /
``test_vt602_retry_with_stable_ids_does_not_crash`` EXACTLY, adapted to the loop's own
``manager.message_ids`` scheme (``(task_id, step_id, attempt)`` instead of ``run_id``): a REAL
langgraph checkpointer + a REAL ``ChatAnthropic._get_request_payload`` build (no network call —
that method only assembles the request) proves that a DBOS-style retry against an
ALREADY-PROGRESSED checkpoint thread does NOT crash when messages are id-scoped per
``manager.message_ids.step_turn_msg_id``, and DOES crash (the control case — proving the harness
actually exercises the real defect) without it.
"""

from __future__ import annotations

from typing import Annotated, TypedDict
from uuid import uuid4

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402

from orchestrator.manager.message_ids import step_thread_id, step_turn_msg_id  # noqa: E402


class _State(TypedDict, total=False):
    messages: Annotated[list, add_messages]


def _specialist_messages_for_anthropic(state_messages: list) -> None:
    """The REAL langchain_anthropic request-payload build (no network call) — the exact validation
    that raises "Received multiple non-consecutive system messages" on a duplicated system block."""
    from langchain_anthropic import ChatAnthropic

    system = SystemMessage(content="you are a specialist")
    ChatAnthropic(model="claude-opus-4-7", max_tokens=16)._get_request_payload(  # type: ignore[call-arg]
        [system, *state_messages]
    )


def _orchestrator_stub(state):
    return {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "spawn_specialist", "args": {}, "id": "tc1"}]),
            ToolMessage(content="handing off", name="spawn_specialist", tool_call_id="tc1"),
        ]
    }


def test_retry_without_stable_ids_reproduces_the_reported_crash() -> None:
    """Control case — proves the harness exercises the real defect: WITHOUT the loop's own
    per-(task_id, step_id, attempt) stable ids, a retry against an already-progressed checkpoint
    reproduces the exact non-consecutive-system-messages crash."""
    task_id, step_id = uuid4(), uuid4()
    thread_id = step_thread_id(task_id, step_id, 1)
    cfg = {"configurable": {"thread_id": thread_id}}

    def _initial_no_stable_ids():
        return {
            "messages": [
                SystemMessage(content="the step's situation"),
                HumanMessage(content="the step's desired outcome"),
            ]
        }

    def _specialist_raises(state):
        raise RuntimeError("attempt 1 fails for some unrelated reason")

    g1 = StateGraph(_State)
    g1.add_node("orchestrator", _orchestrator_stub)
    g1.add_node("specialist", _specialist_raises)
    g1.add_edge(START, "orchestrator")
    g1.add_edge("orchestrator", "specialist")
    g1.add_edge("specialist", END)
    cp = InMemorySaver()
    compiled_1 = g1.compile(checkpointer=cp)
    with pytest.raises(RuntimeError, match="attempt 1 fails"):
        compiled_1.invoke(_initial_no_stable_ids(), config=cfg)

    def _specialist_ok(state):
        _specialist_messages_for_anthropic(state["messages"])
        return {"messages": [AIMessage(content="specialist ran")]}

    g2 = StateGraph(_State)
    g2.add_node("orchestrator", _orchestrator_stub)
    g2.add_node("specialist", _specialist_ok)
    g2.add_edge(START, "orchestrator")
    g2.add_edge("orchestrator", "specialist")
    g2.add_edge("specialist", END)
    compiled_2 = g2.compile(checkpointer=cp)
    with pytest.raises(ValueError, match="non-consecutive system messages"):
        compiled_2.invoke(_initial_no_stable_ids(), config=cfg)


def test_retry_with_stable_ids_does_not_crash() -> None:
    """The fix — the SAME retry-against-a-progressed-checkpoint scenario, but using
    ``manager.message_ids.step_turn_msg_id`` to scope every initial-turn message's id to
    ``(task_id, step_id, attempt)``. Same attempt number on retry (a DBOS step retry replays the
    SAME attempt, per ``manager/workflow.py``'s own discipline — a NEW attempt would get a fresh
    thread_id entirely) -> the SAME ids -> replaced in place -> no crash."""
    task_id, step_id = uuid4(), uuid4()
    attempt = 1
    thread_id = step_thread_id(task_id, step_id, attempt)
    cfg = {"configurable": {"thread_id": thread_id}}

    def _initial_with_stable_ids():
        return {
            "messages": [
                SystemMessage(
                    content="the step's situation",
                    id=step_turn_msg_id(task_id, step_id, attempt, "situation_block"),
                ),
                HumanMessage(
                    content="the step's desired outcome",
                    id=step_turn_msg_id(task_id, step_id, attempt, "human_input"),
                ),
            ]
        }

    def _specialist_raises(state):
        raise RuntimeError("attempt 1 fails for some unrelated reason")

    g1 = StateGraph(_State)
    g1.add_node("orchestrator", _orchestrator_stub)
    g1.add_node("specialist", _specialist_raises)
    g1.add_edge(START, "orchestrator")
    g1.add_edge("orchestrator", "specialist")
    g1.add_edge("specialist", END)
    cp = InMemorySaver()
    compiled_1 = g1.compile(checkpointer=cp)
    with pytest.raises(RuntimeError, match="attempt 1 fails"):
        compiled_1.invoke(_initial_with_stable_ids(), config=cfg)

    ran: list[bool] = []

    def _specialist_ok(state):
        _specialist_messages_for_anthropic(state["messages"])
        ran.append(True)
        return {"messages": [AIMessage(content="specialist ran")]}

    g2 = StateGraph(_State)
    g2.add_node("orchestrator", _orchestrator_stub)
    g2.add_node("specialist", _specialist_ok)
    g2.add_edge(START, "orchestrator")
    g2.add_edge("orchestrator", "specialist")
    g2.add_edge("specialist", END)
    compiled_2 = g2.compile(checkpointer=cp)

    # The retry — SAME thread_id, ids scoped to the SAME (task_id, step_id, attempt) — must NOT
    # raise, and the specialist must actually run (proving the graph made progress, not just
    # silently no-op'd).
    compiled_2.invoke(_initial_with_stable_ids(), config=cfg)
    assert ran == [True]


def test_a_new_attempt_gets_a_fresh_thread_never_touching_the_old_checkpoint() -> None:
    """The OTHER half of amendment A4: a REVISED re-dispatch (attempt+1) must get a completely
    fresh thread — never reusing the failed attempt's checkpoint at all (the VT-602 class: a
    stale attempt's checkpoint must never bleed into a new one)."""
    task_id, step_id = uuid4(), uuid4()
    cp = InMemorySaver()

    def _build():
        g = StateGraph(_State)
        g.add_node("orchestrator", _orchestrator_stub)
        g.add_node("specialist", lambda state: {"messages": [AIMessage(content="ran")]})
        g.add_edge(START, "orchestrator")
        g.add_edge("orchestrator", "specialist")
        g.add_edge("specialist", END)
        return g.compile(checkpointer=cp)

    graph = _build()
    thread_1 = step_thread_id(task_id, step_id, 1)
    cfg_1 = {"configurable": {"thread_id": thread_1}}
    graph.invoke(
        {"messages": [HumanMessage(content="x", id=step_turn_msg_id(task_id, step_id, 1, "human_input"))]},
        config=cfg_1,
    )
    assert cp.get_tuple(cfg_1) is not None  # attempt 1 checkpointed

    thread_2 = step_thread_id(task_id, step_id, 2)
    cfg_2 = {"configurable": {"thread_id": thread_2}}
    assert cp.get_tuple(cfg_2) is None  # attempt 2's thread starts with NO prior checkpoint
