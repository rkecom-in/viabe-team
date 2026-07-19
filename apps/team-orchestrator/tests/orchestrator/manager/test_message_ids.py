"""VT-606 amendment A4 — stable message ids for the loop's checkpointed-thread injections.

Mirrors ``tests/orchestrator/agent/test_dispatch_classify.py``'s VT-602 tests for
``_initial_turn_msg_id``: deterministic per (task_id, step_id, attempt[, slot]); never collides
across attempts (the VT-602 class — a thread must never be reused).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from orchestrator.manager.message_ids import loop_run_id, step_thread_id, step_turn_msg_id


def test_step_thread_id_stable_per_task_step_attempt() -> None:
    task_id, step_id = uuid4(), uuid4()
    assert step_thread_id(task_id, step_id, 1) == step_thread_id(task_id, step_id, 1)


def test_step_thread_id_never_reused_across_attempts() -> None:
    """The VT-602 class: a revised/re-dispatched attempt MUST get a fresh thread_id."""
    task_id, step_id = uuid4(), uuid4()
    assert step_thread_id(task_id, step_id, 1) != step_thread_id(task_id, step_id, 2)


def test_step_thread_id_never_collides_across_steps_or_tasks() -> None:
    task_a, task_b = uuid4(), uuid4()
    step_a, step_b = uuid4(), uuid4()
    assert step_thread_id(task_a, step_a, 1) != step_thread_id(task_b, step_a, 1)
    assert step_thread_id(task_a, step_a, 1) != step_thread_id(task_a, step_b, 1)


def test_step_turn_msg_id_stable_per_full_tuple() -> None:
    task_id, step_id = uuid4(), uuid4()
    assert step_turn_msg_id(task_id, step_id, 1, "human_input") == step_turn_msg_id(
        task_id, step_id, 1, "human_input"
    )


def test_step_turn_msg_id_differs_by_slot_attempt_step_or_task() -> None:
    task_a, task_b = uuid4(), uuid4()
    step_a, step_b = uuid4(), uuid4()
    base = step_turn_msg_id(task_a, step_a, 1, "human_input")
    assert base != step_turn_msg_id(task_a, step_a, 1, "situation_block")  # slot
    assert base != step_turn_msg_id(task_a, step_a, 2, "human_input")  # attempt
    assert base != step_turn_msg_id(task_a, step_b, 1, "human_input")  # step
    assert base != step_turn_msg_id(task_b, step_a, 1, "human_input")  # task


# --- the approval-resume invariant (VT-606 round-3 CRITICAL fix) --------------------------------


def test_loop_run_id_is_a_real_uuid() -> None:
    task_id, step_id = uuid4(), uuid4()
    result = loop_run_id(task_id, step_id, 1)
    assert isinstance(result, UUID)


def test_step_thread_id_equals_str_loop_run_id() -> None:
    """THE invariant request_owner_approval_node / approval_resume.resume_run depend on: the
    graph's checkpoint thread_id and state['run_id'] (workflow.py sets it to loop_run_id directly)
    must be the SAME value — this pins step_thread_id's own contract that it IS that value."""
    task_id, step_id = uuid4(), uuid4()
    assert step_thread_id(task_id, step_id, 3) == str(loop_run_id(task_id, step_id, 3))


def test_loop_run_id_deterministic_across_calls() -> None:
    """Replay-stable (amendment A4): a DBOS step-retry of the SAME attempt must recompute the
    IDENTICAL id, not a fresh uuid4."""
    task_id, step_id = uuid4(), uuid4()
    assert loop_run_id(task_id, step_id, 2) == loop_run_id(task_id, step_id, 2)


def test_loop_run_id_differs_across_attempts() -> None:
    task_id, step_id = uuid4(), uuid4()
    assert loop_run_id(task_id, step_id, 1) != loop_run_id(task_id, step_id, 2)
