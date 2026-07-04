"""VT-565 (B2) — the LIVE producer for manager_tasks / manager_task_steps.

task_store (VT-525) built the durable task/step spine and VT-557/560 the retry ladder + stalled-task
reaper, but nothing on a live run ever CALLED ``create_task`` / ``add_step`` — the table stayed
empty, so the ladder, the ops redrive endpoint and the dead_letter alerts all operated on nothing.
This module is the missing producer: it mints ONE manager_task per objective-bearing dispatch (the
manager delegating to a specialist) and advances it at the run's real seams, so every live run
leaves durable task/step state the existing machinery can act on.

STATE TRACKING ONLY (CL-2026-07-01-observe-only-rails): this writes task/step rows + status
transitions. It NEVER calls ``record_decision`` and NEVER changes what the manager/graph routes —
the routing enforcement rail stays observe-only.

FAIL-SOFT (binding): every write is best-effort — a bookkeeping failure must NEVER break or delay
dispatch/routing. Mirrors ``emit_tm_audit``'s conn=None posture: catch, log WARNING, continue. No
LLM calls (pure SQL bookkeeping via task_store).

Seam map — and WHY a run that dies mid-specialist is walkable by the VT-557/560 reaper:

  * DELEGATE — ``route_after_orchestrator`` returns a SPAWN key (supervisor's producing wrapper) →
    mint the run's task 'planned' then 'running'. NO step yet.
  * COMPLETE — dispatch_brain's successful terminal → add ONE 'done' step (evidence → this
    pipeline_run) + task 'completed'.
  * PAUSE — dispatch_brain's owner-approval ``__interrupt__`` → task 'waiting_owner'. The stalled
    reaper EXCLUDES 'waiting_owner' (and 'clarifying' / 'blocked') from its active-work scan, so an
    awaiting-approval task is NEVER mis-read as stalled and walked to dead_letter.
  * FAIL — dispatch_brain's hard-limit / no-output / escalate terminal → task 'failed'.
  * An unhandled raise touches nothing → the task is left 'running' with NO step, which is EXACTLY
    the reaper's stall predicate (an active-work state with no runnable step) → the ladder retries
    → dead_letter → operator redrive.

The step is only ever written in a TERMINAL status ('done' / 'failed') at the run's terminal, so
there is never a non-terminal ('pending' / 'running' / 'waiting') step shielding a dead run from
the stall reaper. That is the walkability invariant the reaper is designed around.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# route_after_orchestrator's no-spawn sentinel (orchestrator.routing) — a pure-conversational /
# direct-answer turn. Anything else is a specialist spawn = an objective-bearing dispatch.
_TERMINAL_ROUTE = "terminal"


def _run_idem_key(run_id: Any) -> str:
    """The per-run idempotency handle. One objective-bearing dispatch per run (the supervisor graph
    is a single forward pass: orchestrator → one specialist → collapse/END, no loop back), so the
    run identifies the task. Namespaced so it never collides with a source-message idempotency key."""
    return f"live_dispatch:{run_id}"


def on_route_decided(state: Any, route_key: str) -> None:
    """DELEGATE seam. When the orchestrator's route is a specialist SPAWN (anything but the no-spawn
    'terminal'), mint the run's manager_task and move it 'planned' → 'running' (the specialist starts
    the instant routing returns the spawn key). Idempotent per run (idempotency_key = run_id) so a
    re-evaluated conditional edge never double-mints. A pure-conversational turn ('terminal') mints
    nothing. Fully fail-soft — never raises, never alters the route."""
    try:
        if route_key == _TERMINAL_ROUTE:
            return  # no objective-bearing dispatch this turn — the manager answered directly
        tenant_id = state.get("tenant_id")
        run_id = state.get("run_id")
        if tenant_id is None or run_id is None:
            return  # cannot key a durable task without run identity — skip (never raise)
        trigger = state.get("trigger_reason")
        from orchestrator.manager import task_store

        # VT-605 cross-producer duplicate guard: this legacy producer and the NEW plan store
        # (manager/plan_store.create_plan) key a task's IDENTITY differently (this producer uses
        # `live_dispatch:{run_id}`; the plan store uses the inbound source-message SID) but are
        # expected to record the SAME `source_message_ref` pointer for the SAME real-world event.
        # If state carries a `source_message_sid` (populated once VT-606 threads the plan store
        # onto this same dispatch seam) AND a plan-store task already exists for it, this producer
        # must NOT mint a second task for the same event — the plan-store task IS the durable task.
        # `source_message_sid` is absent on every caller TODAY (no live create_plan caller yet), so
        # this branch is a no-op in production until VT-606 wires it — but it is real, tested code,
        # not a placeholder.
        source_message_sid = state.get("source_message_sid")
        if source_message_sid is not None:
            existing_plan_task = task_store.find_task_by_source_ref(tenant_id, source_message_sid)
            if existing_plan_task is not None:
                logger.info(
                    "VT-565 task producer: skipping mint — a plan-store task already exists "
                    "for this event's source_message_sid (task=%s)", existing_plan_task,
                )
                return

        task_id = task_store.create_task(
            tenant_id,
            {"kind": "specialist_dispatch", "route_key": route_key, "trigger": trigger},
            source_message_ref=str(run_id),
            assigned_function=route_key,
            idempotency_key=_run_idem_key(run_id),
            status="planned",
        )
        # planned → running: the delegation is live now. A CAS no-op (already running from a
        # re-fired edge) is fine — set_task_status logs + returns False, never raises.
        task_store.set_task_status(tenant_id, task_id, "running", expected_from=("planned",))
    except Exception:  # noqa: BLE001 — state tracking is best-effort; must never break routing
        logger.warning("VT-565 task producer: mint-on-delegate failed (fail-soft)", exc_info=True)


def on_run_completed(tenant_id: Any, run_id: Any) -> None:
    """SUCCESSFUL-TERMINAL seam. If this run minted a task (a specialist was dispatched), record the
    single 'done' step (evidence → this pipeline_run) and move the task to 'completed'. A no-op when
    no task exists (a conversational turn minted nothing). Fail-soft."""
    _finalize(tenant_id, run_id, task_status="completed", step_status="done",
              detail={"outcome": "completed"})


def on_run_failed(tenant_id: Any, run_id: Any, *, reason: str | None = None) -> None:
    """FAILED-TERMINAL seam (hard-limit abort / specialist-no-output / agent escalation). Record the
    'failed' step + move the task to 'failed' — an honest terminal the reaper leaves alone (there is
    no value in auto-retrying an aborted/escalated run). A no-op when no task exists. Fail-soft."""
    _finalize(tenant_id, run_id, task_status="failed", step_status="failed",
              detail={"outcome": "failed", "reason": reason})


def on_run_paused(tenant_id: Any, run_id: Any) -> None:
    """OWNER-APPROVAL PAUSE seam. Park the run's task at 'waiting_owner' — a state the stalled-task
    reaper EXCLUDES from its active-work scan, so an awaiting-approval task is never mis-read as
    stalled and walked to dead_letter. No step is written (the delegation has not produced a terminal
    outcome yet — the owner is being asked, not told). A no-op when no task exists. Fail-soft."""
    try:
        from orchestrator.manager import task_store

        task_id = task_store.find_task_id(tenant_id, _run_idem_key(run_id))
        if task_id is None:
            return
        task_store.set_task_status(
            tenant_id, task_id, "waiting_owner", expected_from=("running", "planned")
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the pause
        logger.warning("VT-565 task producer: pause transition failed (fail-soft)", exc_info=True)


def _finalize(
    tenant_id: Any, run_id: Any, *, task_status: str, step_status: str, detail: dict[str, Any]
) -> None:
    """Close the run's task: append the single terminal step + move the task terminal. Writing the
    step ONLY in a terminal status (never pending/running/waiting) at the run's terminal is the
    walkability invariant — a run that dies before here leaves an active task with no runnable step,
    which is exactly what the reaper walks. CAS-guarded (``expected_from`` = the non-terminal set) so
    it never regresses a task the reaper already dead-lettered. Fail-soft."""
    try:
        from orchestrator.manager import task_store

        task_id = task_store.find_task_id(tenant_id, _run_idem_key(run_id))
        if task_id is None:
            return  # no objective-bearing dispatch this run — nothing to finalize
        task_store.add_step(
            tenant_id, task_id, 1, "specialist_dispatch",
            evidence_kind="pipeline_run", evidence_ref=str(run_id),
            status=step_status, detail=detail,
        )
        task_store.set_task_status(
            tenant_id, task_id, task_status,
            expected_from=tuple(task_store.TASK_NON_TERMINAL),
            evidence_entry={"kind": "pipeline_run", "ref": str(run_id)},
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the run close
        logger.warning(
            "VT-565 task producer: finalize (%s) failed (fail-soft)", task_status, exc_info=True
        )


__all__ = [
    "on_route_decided",
    "on_run_completed",
    "on_run_failed",
    "on_run_paused",
]
