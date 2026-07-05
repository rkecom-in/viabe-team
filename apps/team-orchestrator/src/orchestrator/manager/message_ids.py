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

VT-606 round-3 CRITICAL fix (adversarial review) — the approval-resume invariant: ``loop_run_id``
is the ONE canonical identity per dispatch attempt. It MUST be used as BOTH the graph's checkpoint
``thread_id`` AND the graph state's own ``run_id`` field. Why: ``request_owner_approval_node``
persists ``state['run_id']`` into ``pending_approvals`` (via ``arm_pause_request``), and
``approval_resume.resume_run`` resumes the SUSPENDED checkpoint with
``thread_id=str(run_id)`` — reading the persisted ``run_id`` back out. Before this fix,
``_dispatch_specialist_step`` used ``step_thread_id(...)`` (a formatted string) as the checkpoint
thread_id but ``UUID(task_id)`` as ``state['run_id']`` — two DIFFERENT values, so an approval
interrupt raised through the loop would persist ``run_id=task_id`` while the ACTUAL checkpoint
lived under a different thread key entirely; the later resume would target a thread that was never
checkpointed, orphaning the approval forever. ``loop_run_id`` is deterministic (``uuid5``, not
``uuid4``) so a DBOS step-retry of the SAME attempt recomputes the IDENTICAL id (replay-stable,
amendment A4 intact) while a NEW attempt (a revise_step re-dispatch) gets a genuinely different one
(never reused across attempts, the VT-602 class) — zero changes needed on the resume side.
"""

from __future__ import annotations

from uuid import NAMESPACE_DNS, UUID, uuid5

# A fixed, deterministic namespace (uuid5 of a DNS name is itself reproducible — not a random
# uuid4 — so this constant is stable across processes/deploys without being hand-picked).
_NAMESPACE = uuid5(NAMESPACE_DNS, "manager-loop.viabe.ai")


def loop_run_id(task_id: UUID | str, step_id: UUID | str, attempt: int) -> UUID:
    """The loop's per-(task_id, step_id, attempt) run identity — THE single value used as both
    the graph's checkpoint ``thread_id`` (via ``step_thread_id``) and the graph state's own
    ``run_id`` field (set directly by ``workflow._dispatch_specialist_step``). See the module
    docstring's CRITICAL-fix note for why these two MUST always be the same value.
    """
    return uuid5(_NAMESPACE, f"manager_task:{task_id}:{step_id}:{attempt}")


def step_thread_id(task_id: UUID | str, step_id: UUID | str, attempt: int) -> str:
    """The dedicated LangGraph ``thread_id`` for ONE specialist dispatch attempt — always
    ``str(loop_run_id(...))`` (never a different value; the approval-resume invariant depends on
    this equality holding exactly).

    NEVER reused across attempts (VT-602 class): a revised/re-dispatched attempt increments
    ``attempt``, which changes ``loop_run_id``, so it always gets a FRESH checkpoint thread — a
    stale attempt's checkpoint can never bleed into a new one.
    """
    return str(loop_run_id(task_id, step_id, attempt))


def step_turn_msg_id(
    task_id: UUID | str, step_id: UUID | str, attempt: int, slot: str
) -> str:
    """A STABLE id for one of THIS step-attempt's initial-turn messages (the HumanMessage /
    SystemMessage(s) manager_task_workflow assembles for a specialist dispatch). Rekeyed off the
    SAME ``loop_run_id`` as ``step_thread_id`` (not the raw tuple) so every identity derived from
    one dispatch attempt traces back to ONE canonical id.

    Same ``(task_id, step_id, attempt, slot)`` -> same id, always — so a DBOS step retry that
    re-enters the SAME attempt's dispatch rebuilds messages that replace themselves in place at
    the checkpoint (``add_messages`` merges by id at the EXISTING index) instead of appending a
    second island. Different slot / different attempt -> a different id (never collides).
    """
    return f"{loop_run_id(task_id, step_id, attempt)}:{slot}"


__all__ = ["loop_run_id", "step_thread_id", "step_turn_msg_id"]
