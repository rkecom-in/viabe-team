"""VT-481 — startup orphan-run reaper.

THE BUG
-------
A ``pipeline_runs`` row is opened ``status='running'`` at the start of a webhook-inbound
workflow and closed (``close_webhook_run``) at the end. If the orchestrator process DIES
mid-run (a Railway deploy-restart, an OOM, a crash), the close never executes → the row is
stranded ``'running'`` FOREVER. DBOS recovery does NOT heal it: on restart DBOS only recovers
workflows matching the *current* ``executor_id`` + ``app_version``, and a redeploy changes the
``app_version``, so a run stranded by the previous deploy is never re-invoked (and so never
closed). 14 such orphans accumulated on dev (some 37 days old, observed VT-481).

WHY A TIME THRESHOLD IS SAFE
----------------------------
A ``running`` row is ONLY the webhook-inbound path, which is hard-bounded: the invoke has a
6-minute timeout (runner) and the pre-dispatch run-control hold caps at 30 min
(``_RUN_CONTROL_MAX_HOLD_S``). The genuinely long-lived states (owner-approval parks, L3
auto-send holds) sit ``status='paused'`` — NOT ``running`` — and are deliberately untouched.
So any run still ``'running'`` well past the longest legitimate ``running`` lifetime is, with
certainty, an orphan from a dead process. We use a conservative 1-hour floor (>> the 30-min max
hold) so this reaper can NEVER race a live in-flight run or a workflow DBOS is mid-recovery on
(DBOS recovery fires within seconds of launch, on same-version rows only).

WHAT IT DOES
------------
At startup (best-effort, after ``launch_dbos()``), close every ``status='running'`` run older
than the threshold to ``status='aborted_hard_limit'`` (an existing terminal CHECK member —
mig 052), stamping ``terminal_state_metadata.reaped_by`` so the close is auditable + greppable.
Service-role (cross-tenant) single UPDATE; never raises (a reaper failure must not block boot).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The floor past which a still-'running' run is certainly orphaned. >> the 30-min run-control
# max hold + the 6-min invoke timeout, so a live in-flight run or a DBOS mid-recovery row is
# never in range. Conservative on purpose (correctness over promptness).
_ORPHAN_AGE_HOURS = 1

# Terminal status for a reaped orphan — an existing pipeline_runs_status_check member (mig 052);
# no schema change. Marks the run aborted (it never completed) rather than faking 'completed'.
_REAPED_STATUS = "aborted_hard_limit"


def _service_pool(pool: Any) -> Any:
    if pool is not None:
        return pool
    from orchestrator.graph import get_pool  # lazy — heavy import chain

    return get_pool()


def reap_orphan_runs(*, pool: Any = None, age_hours: int = _ORPHAN_AGE_HOURS) -> int:
    """Close runs stranded ``status='running'`` older than ``age_hours`` to a terminal status.

    Best-effort + idempotent (re-running only matches still-'running' rows). Returns the number
    of runs reaped. NEVER raises — a reaper failure must not block worker boot (mirrors
    ``warm_pause_cache``). Service-role connection: the sweep is cross-tenant by design (an
    orphan can belong to any tenant), so it does NOT go through the RLS'd tenant_connection.
    """
    try:
        with _service_pool(pool).connection() as conn:
            rows = conn.execute(
                "UPDATE pipeline_runs "
                "SET status = %s, ended_at = now(), "
                # preserve any existing terminal_state_metadata; add the reaper marker.
                "    terminal_state_metadata = "
                "      COALESCE(terminal_state_metadata, '{}'::jsonb) "
                "      || jsonb_build_object('reaped_by', 'vt481_orphan_reaper', "
                "                            'reaped_reason', 'process_died_mid_run', "
                "                            'reaped_at', now()::text) "
                "WHERE status = 'running' "
                "  AND started_at < now() - make_interval(hours => %s) "
                "RETURNING id",
                (_REAPED_STATUS, age_hours),
            ).fetchall()
        n = len(rows)
        if n:
            logger.warning(
                "VT-481 orphan-reaper: closed %d run(s) stranded 'running' >%dh "
                "(process died mid-run; DBOS could not recover a prior-app-version row) -> %s",
                n, age_hours, _REAPED_STATUS,
            )
        else:
            logger.info("VT-481 orphan-reaper: no orphaned 'running' runs to reap")
        return n
    except Exception:  # noqa: BLE001 — best-effort by design; must never block boot
        logger.warning("VT-481 orphan-reaper sweep failed (best-effort)", exc_info=True)
        return 0


# VT-525 (B2): a manager_task is "stalled" if it sits in an ACTIVE-WORK state
# (planned/running/verifying) with no non-terminal step to advance it — i.e. it has no
# runnable step, no durable wait, and no explicit blocker. The deliberate WAIT states
# (clarifying = waiting on the owner's answer, waiting_owner = parked on approval, blocked =
# an explicit blocker already recorded) are EXCLUDED — those are legitimately idle, not
# orphaned. The age floor keeps it clear of a task mid-planning (a step about to be added).
_STALLED_TASK_AGE_HOURS = 1
_STALLED_TASK_ACTIVE_STATES = ("planned", "running", "verifying")


def reap_stalled_manager_tasks(*, pool: Any = None, age_hours: int = _STALLED_TASK_AGE_HOURS) -> int:
    """Apply the VT-557 retry ladder to manager_tasks stranded active with no runnable step.

    The invariant B2 asks for: every non-terminal task has a runnable step, a durable wait, or an
    explicit blocker. A task in planned/running/verifying with NO non-terminal step and no update
    for > ``age_hours`` violates it (a process died between planning and stepping, or a
    step-completion never re-planned). VT-557 turns the old "always → blocked" into a BOUNDED,
    deterministically-backed-off retry ladder (task_retry.decide_retry, reusing backoff.compute_delay):

      * attempt < max_attempts → RETRY: record ``attempt+1`` + ``next_retry_at`` (backoff gate) and
        flip to ``blocked`` (surfaced for review; the reaper skips it until the backoff elapses) →
        orphaned_task alert (VT-529, unchanged for the common single-stall case).
      * attempt reaches max_attempts → DEAD_LETTER: a real retry-exhausted terminal (operator-
        redrivable, never auto-retried again) → dead_letter_task alert (VT-557).

    VT-560 (Defect 1) — WAKE the ladder. VT-557 armed ``next_retry_at`` on the blocked rung but
    nothing ever re-swept 'blocked', so zero auto-retries fired and dead_letter was unreachable via
    the ladder (only the human redrive worked). This sweep now also flips every DUE reaper-parked
    task ('blocked' with ``next_retry_at`` elapsed) back to a runnable 'planned' — keeping the
    incremented ``attempt`` — so a task that stalls again re-enters the ladder and decide_retry
    walks it to dead_letter at the budget. Order matters: the stall sweep runs FIRST, the wake
    SECOND, so a task just blocked this tick (its ``next_retry_at`` is in the future) is NOT
    immediately re-woken and a task just woken to 'planned' is NOT re-scanned by the already-run
    stall query — attempt can never double-increment in one tick, independent of ``age_hours``.

    Best-effort, service-role (cross-tenant), idempotent, NEVER raises.
    """
    try:
        from orchestrator.manager.task_retry import decide_retry

        with _service_pool(pool).connection() as conn:
            candidates = conn.execute(
                "SELECT t.id, t.tenant_id, t.attempt, t.max_attempts, t.status "
                "FROM manager_tasks t "
                "WHERE t.status = ANY(%s) "
                "  AND t.updated_at < now() - make_interval(hours => %s) "
                "  AND (t.next_retry_at IS NULL OR t.next_retry_at < now()) "  # backoff gate
                "  AND NOT EXISTS ( "
                "        SELECT 1 FROM manager_task_steps s "
                "        WHERE s.task_id = t.id "
                "          AND s.status IN ('pending', 'running', 'waiting') "
                "  )",
                (list(_STALLED_TASK_ACTIVE_STATES), age_hours),
            ).fetchall()

            retried: list[Any] = []
            dead_lettered: list[Any] = []
            for row in candidates:
                tid = row["tenant_id"] if isinstance(row, dict) else row[1]
                task_id = row["id"] if isinstance(row, dict) else row[0]
                attempt = int(row["attempt"] if isinstance(row, dict) else row[2])
                max_attempts = int(row["max_attempts"] if isinstance(row, dict) else row[3])
                from_status = row["status"] if isinstance(row, dict) else row[4]
                d = decide_retry(attempt, max_attempts)
                if d.kind == "dead_letter":
                    conn.execute(
                        "UPDATE manager_tasks SET status = 'dead_letter', attempt = %s, "
                        "    next_retry_at = NULL, version = version + 1, updated_at = now(), "
                        "    stall_metadata = COALESCE(stall_metadata, '{}'::jsonb) "
                        "      || jsonb_build_object('reaped_by', 'vt557_retry_ladder', "
                        "         'reaped_reason', 'retry_budget_exhausted', 'reaped_from', %s::text, "
                        "         'attempt', %s::int, 'reaped_at', now()::text) "
                        "WHERE tenant_id = %s AND id = %s",
                        (d.next_attempt, from_status, d.next_attempt, str(tid), str(task_id)),
                    )
                    dead_lettered.append((task_id, tid, d.next_attempt))
                else:
                    conn.execute(
                        "UPDATE manager_tasks SET status = 'blocked', attempt = %s, "
                        "    next_retry_at = now() + make_interval(secs => %s::double precision), "
                        "    version = version + 1, updated_at = now(), "
                        "    stall_metadata = COALESCE(stall_metadata, '{}'::jsonb) "
                        "      || jsonb_build_object('reaped_by', 'vt557_retry_ladder', "
                        "         'reaped_reason', 'no_runnable_step', 'reaped_from', %s::text, "
                        "         'attempt', %s::int, 'reaped_at', now()::text) "
                        "WHERE tenant_id = %s AND id = %s",
                        (d.next_attempt, float(d.delay_s or 0.0), from_status, d.next_attempt,
                         str(tid), str(task_id)),
                    )
                    retried.append((task_id, tid))

            # VT-560 (Defect 1): wake DUE reaper-parked tasks. The ``next_retry_at IS NOT NULL``
            # gate is load-bearing — only the ladder's own parked rows wake automatically; a
            # 'blocked' task with no ``next_retry_at`` (any other blocker semantics — an explicit
            # manager block) is left for a human. CAS on status='blocked' (optimistic-concurrency,
            # the file's pattern); clear next_retry_at; KEEP attempt so the ladder progresses.
            woken = conn.execute(
                "UPDATE manager_tasks SET status = 'planned', next_retry_at = NULL, "
                "    version = version + 1, updated_at = now(), "
                "    stall_metadata = COALESCE(stall_metadata, '{}'::jsonb) "
                "      || jsonb_build_object('woken_by', 'vt560_retry_ladder', "
                "         'woken_reason', 'backoff_elapsed', 'woken_from', 'blocked', "
                "         'attempt', attempt::int, 'woken_at', now()::text) "
                "WHERE status = 'blocked' "
                "  AND next_retry_at IS NOT NULL "
                "  AND next_retry_at <= now() "
                "RETURNING id",
            ).fetchall()

            # VT-668 fix 3 — a dead-lettered task may still hold an OPEN owner-approval: the owner
            # authorized (or is about to authorize) a send that now has no live executor. That death
            # must NOT be silent, and the dangling approval MUST be closed so a LATER owner reply
            # gets the honest-expiry resolution path (VT-668 fix 2), never a resolve-into-nothing on
            # a dead consumer. (With VT-668 fix 1 in place an approval-paused loop task parks
            # 'waiting_owner' and never reaches here — this is the backstop for the legacy
            # task_producer path and any task that armed an approval but stalled for another reason.)
            # For each just-dead-lettered task with an open bound approval: ARM the honest owner
            # stall notification ('not_required' -> 'pending' + terminal_outcome='escalated') and
            # CLOSE the approval (decision='timeout', status='timed_out'). The Twilio send itself
            # fires AFTER this service txn commits (a network send must never hold the sweep's conn).
            from orchestrator.manager import task_store as _task_store

            approval_holders: list[Any] = []
            for _dl_task_id, _dl_tid, _dl_attempt in dead_lettered:
                # conn=None deliberately: the join now lives in PendingApprovalsWrapper (VT-72),
                # whose VT-306 guard rejects this sweep's BYPASSRLS service conn — the wrapper
                # opens its own per-row RLS-scoped tenant_connection instead. The read needs no
                # txn atomicity with the dead-letter writes below (worst case a benign stale read
                # on a rare event); the closes still run on the service conn (allowlisted).
                open_run = _task_store.find_open_approval_run_for_task(_dl_tid, _dl_task_id)
                if open_run is None:
                    continue
                conn.execute(
                    "UPDATE manager_tasks SET terminal_outcome = 'escalated', "
                    "    owner_notification_status = 'pending', version = version + 1, "
                    "    updated_at = now() "
                    "WHERE tenant_id = %s AND id = %s AND owner_notification_status = 'not_required'",
                    (str(_dl_tid), str(_dl_task_id)),
                )
                conn.execute(
                    "UPDATE pending_approvals SET decision = COALESCE(decision, 'timeout'), "
                    "    status = 'timed_out', resolved_at = now() "
                    "WHERE tenant_id = %s AND run_id = %s AND resolved_at IS NULL",
                    (str(_dl_tid), open_run),
                )
                approval_holders.append((_dl_task_id, _dl_tid))

            # VT-668 fix 2 (orphaned awaiting-approval sweep) — a task parked 'waiting_owner'
            # (VT-668 fix 1) whose bound approval has since RESOLVED (the owner replied) but which
            # the loop never consumed: the loop's process died between the reply and the restore
            # (DBOS can't recover a prior-app-version workflow), and the stall-sweep EXCLUDES
            # 'waiting_owner', so nothing else catches this — the exact VT-668 incident shape once
            # fix 1 parks the task. The gate is the APPROVAL's ``resolved_at`` age (NOT the task's
            # updated_at, which reflects park time): a LIVE loop restores the task within its poll
            # cadence (≤300s) of the resolution, so an approval resolved > age_hours ago with the
            # task STILL 'waiting_owner' is, with certainty, a dead consumer. Surface honestly: move
            # to dead_letter (operator-redrivable) + arm the honest owner stall notification. (No
            # auto-send: re-driving a done-step task cannot re-execute the send, and a customer send
            # from the reaper is a money-path action deferred by design — the owner is told the
            # truth and can re-trigger.)
            orphaned = conn.execute(
                "SELECT t.id, t.tenant_id FROM manager_tasks t "
                "JOIN pending_approvals p ON p.tenant_id = t.tenant_id "
                "  AND p.run_id::text = t.stall_metadata->>'awaiting_approval_run_id' "
                "WHERE t.status = 'waiting_owner' AND p.resolved_at IS NOT NULL "
                "  AND p.resolved_at < now() - make_interval(hours => %s)",
                (age_hours,),
            ).fetchall()
            for _o_row in orphaned:
                _o_tid = _o_row["tenant_id"] if isinstance(_o_row, dict) else _o_row[1]
                _o_task = _o_row["id"] if isinstance(_o_row, dict) else _o_row[0]
                conn.execute(
                    "UPDATE manager_tasks SET status = 'dead_letter', terminal_outcome = 'escalated', "
                    "    owner_notification_status = CASE WHEN owner_notification_status = "
                    "        'not_required' THEN 'pending' ELSE owner_notification_status END, "
                    "    version = version + 1, updated_at = now(), "
                    "    stall_metadata = COALESCE(stall_metadata, '{}'::jsonb) "
                    "      || jsonb_build_object('reaped_by', 'vt668_orphaned_approval', "
                    "         'reaped_reason', 'approval_resolved_no_consumer', 'reaped_at', now()::text) "
                    "WHERE tenant_id = %s AND id = %s AND status = 'waiting_owner'",
                    (str(_o_tid), str(_o_task)),
                )
                approval_holders.append((_o_task, _o_tid))

        n = len(candidates)
        n_orphaned = len(orphaned)
        n_woken = len(woken)
        if n_woken:
            logger.warning(
                "VT-560 retry-ladder wake: %d reaper-parked task(s) woken blocked->planned "
                "(backoff elapsed; re-enter the stall ladder if still no runnable step)", n_woken,
            )
        if n:
            logger.warning(
                "VT-557 retry-ladder reaper: %d stalled task(s) — %d retried (blocked+backoff), "
                "%d dead-lettered", n, len(retried), len(dead_lettered),
            )
            # VT-529 orphaned_task for the retried (still surfaced); VT-557 dead_letter_task for the
            # exhausted. Fail-soft per alert + dev-routed (a dev/canary tenant never pages Fazal).
            _alert_orphaned_tasks([{"id": t, "tenant_id": g} for t, g in retried])
            _alert_dead_letter_tasks(dead_lettered)
        if n_orphaned:
            logger.warning(
                "VT-668 orphaned-approval sweep: %d 'waiting_owner' task(s) whose approval resolved "
                "with no live consumer -> dead_letter + honest owner notify", n_orphaned,
            )
        # VT-668 — POST-commit: the dead_letter + notify-arm + approval-close (fix 3) and the
        # orphaned-approval surfacing (fix 2b) already committed above, so each honest owner stall
        # notification lands on a durable 'pending' row (its delivered-flip is a real second
        # backstop). Fires whenever there is ANY approval-holder to surface, independent of the
        # stall-candidate count. Fail-soft per task.
        if approval_holders:
            _notify_approval_holders(approval_holders)
        if not n and not n_woken and not n_orphaned:
            logger.info("VT-557 retry-ladder reaper: no stalled or wakeable manager_tasks")
        return n
    except Exception:  # noqa: BLE001 — best-effort by design; must never block boot
        logger.warning("VT-557 retry-ladder reaper sweep failed (best-effort)", exc_info=True)
        return 0


def _alert_orphaned_tasks(rows: Any) -> None:
    """Fire one ``orphaned_task`` alert per reaped task (ops visibility). Each dispatch is
    fail-soft + dev-routed (a dev/canary tenant never pages Fazal). ``rows`` carry (id, tenant_id)."""
    try:
        from uuid import UUID

        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for
    except Exception:  # noqa: BLE001 — alerts import must never break the reaper
        logger.warning("VT-529 orphaned_task alert import failed (fail-soft)", exc_info=True)
        return
    for row in rows:
        try:
            tid = row["tenant_id"] if isinstance(row, dict) else row[1]
            task_id = row["id"] if isinstance(row, dict) else row[0]
            tenant_uuid = tid if isinstance(tid, UUID) else UUID(str(tid))
            dispatch_alert(Trigger(
                tenant_id=tenant_uuid,
                trigger_kind="orphaned_task",
                severity=severity_for("orphaned_task"),
                message_text=(
                    f"Manager task {task_id} was stranded active with no runnable step and reaped "
                    "to 'blocked' (no runnable step / durable wait / explicit blocker). Investigate."
                ),
                payload={"task_id": str(task_id), "reaped_reason": "no_runnable_step"},
            ))
        except Exception:  # noqa: BLE001 — one alert failing must not stop the rest or the reaper
            logger.warning("VT-529 orphaned_task alert dispatch failed (fail-soft)", exc_info=True)


def _alert_dead_letter_tasks(rows: Any) -> None:
    """VT-557 — fire one ``dead_letter_task`` alert per retry-exhausted task (an operator must
    redrive it). ``rows`` carry (task_id, tenant_id, attempt). Fail-soft per task + dev-routed."""
    try:
        from uuid import UUID

        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for
    except Exception:  # noqa: BLE001 — alerts import must never break the reaper
        logger.warning("VT-557 dead_letter_task alert import failed (fail-soft)", exc_info=True)
        return
    for task_id, tid, attempt in rows:
        try:
            tenant_uuid = tid if isinstance(tid, UUID) else UUID(str(tid))
            dispatch_alert(Trigger(
                tenant_id=tenant_uuid,
                trigger_kind="dead_letter_task",
                severity=severity_for("dead_letter_task"),
                message_text=(
                    f"Manager task {task_id} exhausted its retry budget (attempt {attempt}) and was "
                    "dead-lettered. It will NOT auto-retry — an operator must redrive it "
                    "(ops/run-control/redrive-task) after investigating the stall cause."
                ),
                payload={"task_id": str(task_id), "attempt": attempt,
                         "reaped_reason": "retry_budget_exhausted"},
            ))
        except Exception:  # noqa: BLE001 — one alert failing must not stop the rest or the reaper
            logger.warning("VT-557 dead_letter_task alert dispatch failed (fail-soft)", exc_info=True)


def _notify_approval_holders(rows: Any) -> None:
    """VT-668 fix 3 — fire the honest owner stall notification for each dead-lettered task that held
    an OPEN owner-approval (armed 'pending' + terminal_outcome='escalated' in the committed sweep
    txn above). Reuses the SAME VT-611 owner-notification composer the workflow tail uses
    (``maybe_notify_owner_of_task_outcome`` — idempotent on ``owner_notification_status``, fail-soft,
    honest 'I couldn't complete it on my own' copy for the 'escalated' outcome). Post-commit + fail-
    soft per task: a notify failure must never break the reaper. ``rows`` carry (task_id, tenant_id)."""
    try:
        from orchestrator.owner_surface.task_outcome import maybe_notify_owner_of_task_outcome
    except Exception:  # noqa: BLE001 — the notifier import must never break the reaper
        logger.warning("VT-668 approval-holder notify import failed (fail-soft)", exc_info=True)
        return
    for task_id, tid in rows:
        try:
            maybe_notify_owner_of_task_outcome(tid, task_id)
        except Exception:  # noqa: BLE001 — one notify failing must not stop the rest or the reaper
            logger.warning(
                "VT-668 approval-holder notify failed (fail-soft) task=%s", task_id, exc_info=True
            )


_SILENT_TERMINAL_AGE_MINUTES = 30


def detect_silent_terminal_runs(
    *, pool: Any = None, age_minutes: int = _SILENT_TERMINAL_AGE_MINUTES
) -> int:
    """VT-552 (B1 part-2b): find runs that reached ``status='completed'`` with NO ``final_outcome``
    (a SILENT TERMINAL — ended clean but produced nothing the owner/ops can see), open a durable
    ``silent_terminal`` incident per run (idempotent), and fire the ``silent_terminal`` alert.

    Best-effort, cross-tenant, never raises (a detector failure must not block boot). ``age_minutes``
    (>> a normal completed run's settle time) avoids racing a run whose ``final_outcome`` write is
    just in flight.

    VT-560 review follow-up: the predicate honors BOTH outcome substrates — the mig-025
    ``final_outcome`` COLUMN and the mig-052 house-pattern ``terminal_state_metadata->>
    'final_outcome'`` JSONB key (what rerun/coordinator actually write; the column has no live
    writer). NOTE: the close path (``close_webhook_run``) stamps NEITHER, so most completed
    webhook runs genuinely match this predicate — which is why this detector is deliberately NOT
    on the @DBOS.scheduled substrate (under traffic it would open an incident + alert per
    completed run every tick). It stays a boot-time catch-up until the close-path final_outcome
    writer lands (rostered follow-up); schedule it only after that."""
    try:
        with _service_pool(pool).connection() as conn:
            rows = conn.execute(
                "SELECT r.id, r.tenant_id FROM pipeline_runs r "
                "WHERE r.status = 'completed' "
                "  AND NULLIF(btrim(COALESCE("
                "        r.final_outcome, r.terminal_state_metadata->>'final_outcome', '')), '') "
                "      IS NULL "
                "  AND r.ended_at IS NOT NULL "
                "  AND r.ended_at < now() - make_interval(mins => %s) "
                "  AND NOT EXISTS (SELECT 1 FROM incidents i "
                "                  WHERE i.run_id = r.id AND i.incident_kind = 'silent_terminal') "
                "LIMIT 500",
                (age_minutes,),
            ).fetchall()
            opened = 0
            from orchestrator.observability.incident_store import create_incident

            for row in rows:
                tid = row["tenant_id"] if isinstance(row, dict) else row[1]
                rid = row["id"] if isinstance(row, dict) else row[0]
                # Service conn bypasses RLS → the tenant-scoped incident INSERT works with explicit tid.
                inc = create_incident(
                    tid, incident_kind="silent_terminal", run_id=rid,
                    detail={"detector": "vt552_silent_terminal", "age_minutes": age_minutes},
                    conn=conn,
                )
                if inc is not None:
                    opened += 1
        if opened:
            logger.warning(
                "VT-552 silent-terminal detector: opened %d incident(s) for runs completed with "
                "no final_outcome (>%dm)", opened, age_minutes,
            )
            _alert_silent_terminals(rows)
        else:
            logger.info("VT-552 silent-terminal detector: none")
        return opened
    except Exception:  # noqa: BLE001 — detector must never break boot
        logger.warning("VT-552 silent-terminal detector failed (fail-soft)", exc_info=True)
        return 0


def _alert_silent_terminals(rows: Any) -> None:
    """Fire one ``silent_terminal`` alert per detected run (fail-soft + dev-routed)."""
    try:
        from uuid import UUID

        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for
    except Exception:  # noqa: BLE001
        logger.warning("VT-552 silent_terminal alert import failed (fail-soft)", exc_info=True)
        return
    for row in rows:
        try:
            tid = row["tenant_id"] if isinstance(row, dict) else row[1]
            rid = row["id"] if isinstance(row, dict) else row[0]
            dispatch_alert(Trigger(
                tenant_id=tid if isinstance(tid, UUID) else UUID(str(tid)),
                trigger_kind="silent_terminal",
                severity=severity_for("silent_terminal"),
                message_text=(
                    f"Run {rid} completed with no final outcome and no owner contact "
                    "(silent terminal) — incident opened. Investigate / escalate."
                ),
                payload={"run_id": str(rid)},
            ))
        except Exception:  # noqa: BLE001
            logger.warning("VT-552 silent_terminal alert dispatch failed (fail-soft)", exc_info=True)


__all__ = [
    "reap_orphan_runs",
    "reap_stalled_manager_tasks",
    "detect_silent_terminal_runs",
]
