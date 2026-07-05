"""VT-606 amendment A4 — stable message ids for the manager loop's checkpointed-thread injections.

Mirrors ``agent.dispatch._initial_turn_msg_id`` (VT-602) EXACTLY, scoped to the loop's own
identifier tuple instead of ``run_id``. The VT-602 defect: a message built with NO id (or a
FRESH id every call) gets assigned/keeps a NEW identity on every graph invocation; LangGraph's
``add_messages`` reducer keys purely on ``BaseMessage.id``, so a DBOS retry (or, here, a step
re-dispatch) that rebuilds the SAME logical message against an ALREADY-PROGRESSED checkpoint
thread APPENDS a duplicate instead of replacing it in place — the exact shape
``langchain_anthropic`` rejects as "Received multiple non-consecutive system messages."

Loop-specific identifier: EVERY specialist dispatch gets its OWN thread_id (never reused across
attempts — "each specialist dispatch = one graph invocation ... NEVER reuse a thread_id across
attempts, the VT-602 class"), so the thread_id itself already changes per attempt. The remaining
risk this module closes is WITHIN one thread_id: if ``manager_task_workflow`` re-enters the SAME
step dispatch (a DBOS step retry replaying the same attempt after a mid-dispatch crash, NOT a new
attempt), the freshly-rebuilt initial-turn messages must replace themselves in place rather than
append — hence scoping every injected message's id to ``(task_id, step_id, attempt, slot)``.
"""

from __future__ import annotations

from uuid import UUID


def step_thread_id(task_id: UUID | str, step_id: UUID | str, attempt: int) -> str:
    """The dedicated LangGraph ``thread_id`` for ONE specialist dispatch attempt.

    NEVER reused across attempts (VT-602 class): a revised/re-dispatched attempt increments
    ``attempt``, which changes this string, so it always gets a FRESH checkpoint thread — a stale
    attempt's checkpoint can never bleed into a new one.
    """
    return f"manager_task:{task_id}:{step_id}:{attempt}"


def step_turn_msg_id(
    task_id: UUID | str, step_id: UUID | str, attempt: int, slot: str
) -> str:
    """A STABLE id for one of THIS step-attempt's initial-turn messages (the HumanMessage /
    SystemMessage(s) manager_task_workflow assembles for a specialist dispatch).

    Same ``(task_id, step_id, attempt, slot)`` -> same id, always — so a DBOS step retry that
    re-enters the SAME attempt's dispatch rebuilds messages that replace themselves in place at
    the checkpoint (``add_messages`` merges by id at the EXISTING index) instead of appending a
    second island. Different slot / different attempt -> a different id (never collides).
    """
    return f"manager_task:{task_id}:{step_id}:{attempt}:{slot}"


__all__ = ["step_thread_id", "step_turn_msg_id"]
