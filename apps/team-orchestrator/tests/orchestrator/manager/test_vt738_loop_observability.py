"""VT-738 — the enforce loop's per-node trace must be writable, and its route decisions recorded.

These pin the two things whose ABSENCE made the delegation miss unattributable. Both are the kind
of defect that leaves no trace of itself: a swallowed FK violation and an early `return` look
exactly like "this code path did not run", so nothing failed and nobody noticed for weeks.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/orchestrator/manager/<this> -> parents[3] is the app root (team-orchestrator).
SRC = Path(__file__).resolve().parents[3] / "src" / "orchestrator"


def test_the_loop_observability_fk_defect_is_documented_where_someone_would_hit_it() -> None:
    """The loop's `pipeline_steps` writes FK-violate and are swallowed — a real, OPEN defect.

    It is not fixed here because the one-token fix (open the context under `run_id`) repoints
    `ctx.run_id` for everything inside the graph, including the identity campaigns are created
    under. This test does not assert the defect is fixed — a permanently-red test is a broken
    window, not a gate. It asserts the WARNING survives, so the next person who spots the
    mismatch does not "fix" it without seeing the blast radius.
    """
    source = (SRC / "manager" / "workflow.py").read_text()

    ctx = re.search(r"with observability_context\(\s*run_id=([^,]+),", source)
    assert ctx, "could not find the observability_context the dispatch runs inside"

    if ctx.group(1).strip() == "UUID(task_id)":
        assert "campaigns.run_id" in source, (
            "the context is still opened under the task id (the known FK defect), so the comment "
            "explaining WHY it was not naively fixed must stay next to it"
        )


def test_manager_review_join_is_pinned_to_the_task_not_borrowed_from_the_context() -> None:
    """The §7D join target is the manager task id. It must be written explicitly rather than read
    off the ObservabilityContext, which now carries the dispatch run instead."""
    source = (SRC / "supervisor.py").read_text()

    assert "review_run_id = task_id" in source, (
        "manager_review's reasoning_ref must be pinned to the manager task id explicitly"
    )
    assert "_observability_context.get()" not in source, (
        "supervisor must no longer borrow the join key from the ObservabilityContext — that "
        "coupling only worked while the context was (accidentally) opened under the task id"
    )


def test_route_decided_carries_the_loop_join_keys() -> None:
    """A `route_decided` row on an enforce-loop dispatch is keyed on `loop_run_id(task, step,
    attempt)` — a hash nothing can invert. Without the task/step ids in the payload the row cannot
    be tied back to the task whose delegation it decided, so misses stay countable-but-unattributable.
    """
    source = (SRC / "routing.py").read_text()

    assert source.count("**_loop_join_keys(state)") == 2, (
        "BOTH route_decided emits (spawn and terminal) must carry the loop join keys — the "
        "terminal one is the delegation MISS, so omitting it defeats the purpose"
    )


def test_prereq_failure_is_not_filed_under_the_limit_exceeded_kind() -> None:
    """Two unrelated block causes shared one `event_kind`, so "why did this task block" could not
    be answered from the audit log alone."""
    source = (SRC / "manager" / "workflow.py").read_text()

    assert 'event_kind="manager_task_prereq_failed"' in source
    assert source.count('event_kind="manager_task_limit_exceeded"') == 1, (
        "manager_task_limit_exceeded must be emitted by the limit path ONLY"
    )


def test_defaulted_escalate_is_recorded() -> None:
    """The loop defaulting to `escalate` because `manager_review` never ran is the exact path that
    produces the owner-facing "I couldn't complete it on my own" closure with no spawn behind it.
    It wrote no audit row and no incident, which is why it was indistinguishable from a genuine
    manager_review escalation and from an activation-gate fail-closed."""
    source = (SRC / "manager" / "workflow.py").read_text()

    assert 'event_kind="manager_task_escalate_defaulted"' in source
    assert "terminated_without_spawn" in source, (
        "the row must record whether the brain emitted no spawn tool — that flag is what separates "
        "'the Manager did not delegate' from 'manager_review was skipped for another reason'"
    )
