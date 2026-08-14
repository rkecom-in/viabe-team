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

            approval_holders: list[Any] = []
            for _dl_task_id, _dl_tid, _dl_attempt in dead_lettered:
                # Inline tenant-predicated join on the sweep's OWN service conn — this file is the
                # allowlisted BYPASSRLS cross-tenant sweep (VT-72 allowlist entry), and the wrapper
                # path is unusable here twice over: its VT-306 guard rejects a non-app_role conn,
                # and conn=None would open tenant_connection → the global pool, which is not
                # initialized in the reaper's context (init_substrate is a service-boot concern).
                # Same two linkages as PendingApprovalsWrapper.open_run_for_task (the resolution
                # seam keeps the wrapper — it holds a real app_role conn).
                _row = conn.execute(
                    "SELECT p.run_id::text AS run_id FROM pending_approvals p "
                    "JOIN manager_tasks t ON t.tenant_id = p.tenant_id "
                    "WHERE t.tenant_id = %s AND t.id = %s AND p.resolved_at IS NULL "
                    "  AND (t.stall_metadata->>'awaiting_approval_run_id' = p.run_id::text "
                    "       OR t.source_message_ref = p.run_id::text) "
                    "ORDER BY p.requested_at DESC LIMIT 1",
                    (str(_dl_tid), str(_dl_task_id)),
                ).fetchone()
                open_run = None if _row is None else str(
                    _row["run_id"] if isinstance(_row, dict) else _row[0]
                )
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
    except Exception:  # noqa: BLE001 — best-effort by design; must never block boot
        logger.warning("VT-557 retry-ladder reaper sweep failed (best-effort)", exc_info=True)
        return 0
    finally:
        # VT-740 — the crashed-campaign terminalizer runs on THIS sweep's schedule
        # (STALLED_TASK_SWEEP_CRON, every 10 min) rather than as a 12th registered cron. It has
        # its own connection + its own try/except, so it can neither see nor corrupt the ladder's
        # transaction above, and a failure here can never change the ladder's result. In `finally`
        # deliberately: a ladder failure is not a reason to leave a crashed campaign stranded
        # 'approved' forever — the two sweeps are independent.
        reap_crashed_campaigns(pool=pool)
    return n


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


# ---------------------------------------------------------------------------
# VT-740 — the crashed-campaign terminalizer.
#
# WHY THIS EXISTS. ``campaign/execute.py`` advances a campaign to 'sent' only AFTER the whole
# fan-out loop returns, and the only other writer is the VT-558 operator cancel. So a campaign
# whose executor DIED at recipient 40 of 100 stays ``status='approved'`` FOREVER: nothing ever
# moves it, its remainder is never surfaced, and any effect-state predicate of the form "does this
# tenant have an approved campaign that already delivered?" stays permanently TRUE. That last
# property is what killed the second wake-gate attempt (VT-740): the condition the gate fired on
# was the condition that never cleared, so every protected tenant would eventually wedge with
# ``blocked`` (∈ TASK_ACTIVE) holding its one active slot. Terminalizing the campaign is the fix
# for the ROOT of that, and it is the shape Clau ratified over a third wake gate.
#
# WHY 'failed' AND NOT 'sent'. The sweep cannot prove a campaign COMPLETED. ``remainder == 0`` is
# not evidence: a recipient skipped for opt-out / complaint-freeze / frequency is processed
# correctly and is never "delivered", so a fully-processed campaign and one that died on its last
# recipient can look identical from the ledger. Claiming 'sent' would tell the owner and the VTR
# that a campaign finished when it may not have — the dangerous direction. 'failed' claims nothing
# about delivery; the real intended/delivered/remainder numbers reach the VTR through
# ``prod_workflow_diagnosis`` (which reads the SAME ledger join used below).
#
# WHY THIS CANNOT KILL A LIVE FAN-OUT. Three independent guards: (1) the candidate must ALREADY
# have written at least one ``campaign_messages`` row (``last_message_at IS NOT NULL``) — a
# campaign that has not started is never touched; (2) that last row must be older than
# ``_CRASHED_CAMPAIGN_AGE_HOURS``, and the fan-out loop has NO pacing/sleep — it writes a ledger
# row per recipient back-to-back — so a two-hour gap is not a slow campaign, it is a dead one;
# (3) the UPDATE re-checks, at write time, that no message newer than the observed
# ``last_message_at`` has appeared, which closes the read→write race with a loop that resumed in
# between. The UPDATE is additionally CAS'd on ``status = 'approved'``, so a loop that reached its
# own status advance first wins.
_CRASHED_CAMPAIGN_AGE_HOURS = 2
_CRASHED_CAMPAIGN_LIMIT = 200
_CRASHED_CAMPAIGN_TERMINAL = "failed"

# The campaign→message link, SHARED by the candidate SELECT (dict params) and the terminalize
# UPDATE (dict params).
#
# ``starts_with(a, b)`` here is load-bearing, not style. The reverted second attempt shared a
# fragment containing ``LIKE c.id::text || ':%'`` between a no-param query and a param-carrying
# one: psycopg parses ``%`` ONLY when ``params is not None``, so the fragment parsed fine in one
# caller and raised ``ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders`` on
# EVERY call in the other — gate live, safety valve dead. Doubling to ``%%`` inverts the bug
# instead of fixing it: with ``params=None`` psycopg sends the string VERBATIM, so the server sees
# a literal ``%%`` and LIKE matches a literal percent sign — silently zero rows, the exact
# "inert gate" class that killed attempt #1. ``starts_with`` contains no ``%`` at all and is
# therefore correct in BOTH modes. Proved against the pinned psycopg in
# tests/orchestrator/test_vt740_crashed_campaign_terminalization.py.
#
# The first arm (``m.campaign_id = c.id``) is the real column, populated at write time since
# VT-740; the second arm covers rows written BEFORE that, which are permanently NULL and cannot be
# back-filled honestly. Same two arms as ``CampaignsWrapper.effect_state_rollup``, so the reaper
# and the VTR diagnosis count the same sends.
_CAMPAIGN_MSG_LINK = (
    "(m.campaign_id = c.id "
    "     OR (m.campaign_id IS NULL "
    "         AND starts_with(m.idempotency_key, c.id::text || ':')))"
)

_CRASHED_CAMPAIGN_CANDIDATES_SQL = (
    "SELECT c.id::text AS campaign_id, c.tenant_id::text AS tenant_id, "
    "       ms.delivered, ms.attempted, ms.last_message_at, "
    "       (SELECT count(*) FROM campaign_recipients r "
    "         WHERE r.tenant_id = c.tenant_id AND r.campaign_id = c.id) AS intended "
    "  FROM campaigns c "
    "  JOIN LATERAL ( "
    "        SELECT count(*) FILTER (WHERE m.send_status = ANY(%(delivered)s)) AS delivered, "
    "               count(*) FILTER (WHERE NOT (m.send_status = ANY(%(delivered)s))) AS attempted, "
    "               max(m.created_at) AS last_message_at "
    "          FROM campaign_messages m "
    "         WHERE m.tenant_id = c.tenant_id "
    "           AND " + _CAMPAIGN_MSG_LINK + " "
    "       ) ms ON TRUE "
    " WHERE c.status = 'approved' "
    "   AND ms.last_message_at IS NOT NULL "
    "   AND ms.last_message_at < now() - make_interval(hours => %(age_hours)s) "
    " ORDER BY ms.last_message_at "
    " LIMIT %(limit)s"
)

_TERMINALIZE_CRASHED_CAMPAIGN_SQL = (
    "UPDATE campaigns AS c SET status = %(terminal)s "
    " WHERE c.tenant_id = %(tenant)s AND c.id = %(campaign)s AND c.status = 'approved' "
    # Read→write race guard: if the (presumed dead) fan-out wrote another ledger row after the
    # candidate SELECT observed ``last_message_at``, it is alive — leave it alone.
    "   AND NOT EXISTS (SELECT 1 FROM campaign_messages m "
    "                    WHERE m.tenant_id = c.tenant_id "
    "                      AND " + _CAMPAIGN_MSG_LINK + " "
    "                      AND m.created_at > %(observed_last)s) "
    "RETURNING c.id::text"
)


def reap_crashed_campaigns(
    *, pool: Any = None, age_hours: int = _CRASHED_CAMPAIGN_AGE_HOURS,
    limit: int = _CRASHED_CAMPAIGN_LIMIT,
) -> int:
    """VT-740 — move a crashed, partially-executed campaign to a TERMINAL status and ALERT.

    Returns the number of campaigns terminalized. Best-effort, cross-tenant (service-role — a
    stranded campaign can belong to any tenant, and a sweep cannot run inside one tenant's RLS
    scope; this file is the VT-72 allowlisted sweep). NEVER raises.

    This CONTAINS and REPORTS; it does not resolve. It never re-sends, never cancels the
    remainder, and never claims a campaign completed — the VTR decides what happens to the
    customers who were never messaged (Fazal 2026-07-10: no blind re-run, no silent disable).
    """
    try:
        from orchestrator.prod_workflow_diagnosis import _DELIVERED
    except Exception:  # noqa: BLE001 — never let an import break the reaper
        # A second literal ('sent', 'template_sent') here would be a SECOND definition of
        # "delivered" that can drift from the one the VTR diagnosis reads. Fail closed instead:
        # without it we cannot say what reached a customer, so we terminalize nothing.
        logger.warning("VT-740 crashed-campaign sweep: delivered-status import failed", exc_info=True)
        return 0
    try:
        terminalized: list[dict[str, Any]] = []
        with _service_pool(pool).connection() as conn:
            candidates = conn.execute(
                _CRASHED_CAMPAIGN_CANDIDATES_SQL,
                {"delivered": list(_DELIVERED), "age_hours": age_hours, "limit": limit},
            ).fetchall()
            for row in candidates:
                r = dict(row) if isinstance(row, dict) else {
                    "campaign_id": row[0], "tenant_id": row[1], "delivered": row[2],
                    "attempted": row[3], "last_message_at": row[4], "intended": row[5],
                }
                updated = conn.execute(
                    _TERMINALIZE_CRASHED_CAMPAIGN_SQL,
                    {
                        "terminal": _CRASHED_CAMPAIGN_TERMINAL,
                        "tenant": str(r["tenant_id"]),
                        "campaign": str(r["campaign_id"]),
                        "observed_last": r["last_message_at"],
                        "delivered": list(_DELIVERED),
                    },
                ).fetchall()
                if updated:
                    terminalized.append(r)
        if terminalized:
            logger.warning(
                "VT-740 crashed-campaign sweep: terminalized %d campaign(s) stranded 'approved' "
                "with no ledger progress for >%dh -> '%s' (remainder is NOT auto-resolved; the "
                "VTR decides)", len(terminalized), age_hours, _CRASHED_CAMPAIGN_TERMINAL,
            )
            _alert_crashed_campaigns(terminalized)
        else:
            logger.info("VT-740 crashed-campaign sweep: none")
        return len(terminalized)
    except Exception:  # noqa: BLE001 — best-effort by design; must never break the reaper
        logger.warning("VT-740 crashed-campaign sweep failed (best-effort)", exc_info=True)
        return 0


def _alert_crashed_campaigns(rows: Any) -> None:
    """VT-740 — one durable ``tenant_alerts`` row per terminalized campaign.

    A ``logger.warning`` pages nobody: it is pull-only, and a partially-sent campaign whose
    remainder was never messaged needs a human TODAY. ``dispatch_alert`` writes the row (Ops
    Console + VTR surfaces read it) and fires Telegram/email, dev-routed so a dev/canary tenant
    never pages Fazal.

    Severity is the effect: a campaign that already DELIVERED to real people is an 'escalation'
    (critical — real customers are half-messaged and a human must decide the remainder); one that
    delivered nothing is a 'silent_terminal' (warning — it ended with no effect and the owner was
    never told). Both kinds are existing ``tenant_alerts_trigger_kind_check`` members (mig 172);
    a new kind would need a migration, and this lane allocates none.

    CL-390: counts + ids only — never a customer id, phone, or message body. Fail-soft per row.
    """
    try:

        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for
    except Exception:  # noqa: BLE001 — alerts import must never break the reaper
        logger.warning("VT-740 crashed-campaign alert import failed (fail-soft)", exc_info=True)
        return
    # AGGREGATE PER TENANT — one alert carrying every campaign, NOT one alert per campaign.
    #
    # `alerts.dispatch._dedup_key` is `f"{tenant_id}:{trigger_kind}"` on a 5-minute window, and it
    # is NOT campaign-scoped. This sweep terminalizes up to _CRASHED_CAMPAIGN_LIMIT (200) campaigns
    # in one sub-second tick, so a per-campaign loop would fire alert #1 and have every other one
    # silently deduped away.
    #
    # That would be a REGRESSION IN RECOVERABILITY, not just a missed notification. The terminal
    # status IS the idempotency key (the CAS is on `status = 'approved'`), so a campaign flipped to
    # 'failed' is never a candidate again — un-alerted and no longer findable by the
    # "approved with no ledger progress" shape that used to surface it. Half-messaged real customer
    # cohorts would be silently forgotten, which is the exact harm this sweep exists to surface.
    # The guaranteed trigger is the sweep's FIRST production run, which clears the whole historical
    # backlog at once.
    by_tenant: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_tenant.setdefault(str(r["tenant_id"]), []).append(r)

    for tenant_key, group in by_tenant.items():
        try:
            from uuid import UUID as _UUID

            per_campaign = []
            for r in group:
                delivered = int(r.get("delivered") or 0)
                intended = int(r.get("intended") or 0)
                per_campaign.append({
                    "campaign_id": str(r["campaign_id"]),
                    "intended": intended,
                    "delivered": delivered,
                    "attempted_not_delivered": int(r.get("attempted") or 0),
                    "remainder": max(0, intended - delivered),
                })
            reached = [c for c in per_campaign if c["delivered"]]
            total_remainder = sum(c["remainder"] for c in per_campaign)
            # 'escalation' the moment ANY of them reached a real person — the worst case in the
            # group decides, because that is the one that needs a human today.
            kind = "escalation" if reached else "silent_terminal"
            plural = "s" if len(per_campaign) != 1 else ""
            dispatch_alert(Trigger(
                tenant_id=_UUID(tenant_key),
                trigger_kind=kind,
                severity=severity_for(kind),
                message_text=(
                    f"{len(per_campaign)} campaign{plural} were stranded 'approved' with a dead "
                    f"executor and have been terminalized to '{_CRASHED_CAMPAIGN_TERMINAL}'. "
                    f"{len(reached)} of them had ALREADY delivered to real customers; "
                    f"{total_remainder} customer(s) across the group were NEVER messaged. "
                    "Nothing was auto-resolved and nothing was re-run (re-running would re-message "
                    "everyone already contacted). Work them on the failed-workflow diagnosis view. "
                    f"Campaign ids: {', '.join(c['campaign_id'] for c in per_campaign)}"
                ),
                payload={
                    "campaigns": per_campaign,
                    "campaign_count": len(per_campaign),
                    "campaigns_that_reached_customers": len(reached),
                    "total_remainder": total_remainder,
                    "terminalized_to": _CRASHED_CAMPAIGN_TERMINAL,
                    "reaped_by": "vt740_crashed_campaign_sweep",
                },
            ))
        except Exception:  # noqa: BLE001 — one tenant's alert must not stop the rest or the reaper
            logger.warning(
                "VT-740 crashed-campaign alert dispatch failed for one tenant (fail-soft)",
                exc_info=True,
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


#: VT-755 — how long a task may sit 'waiting_owner' with no wake path before it is a WEDGE rather than
#: a race. Deliberately generous: the legitimate approval park runs to a 48h TTL, and this detector
#: must never fire on one of those (it can't — the predicate requires no open approval AND no wake
#: stamp — but the grace makes a mid-park write in flight impossible to catch either).
_WEDGE_AGE_HOURS = 2


def detect_wedged_tenants(*, pool: Any = None, age_hours: int = _WEDGE_AGE_HOURS) -> int:
    """VT-755 — find tenants whose Manager is WEDGED and page a human. Returns the count found.

    A task parked ``waiting_owner`` is UNREACHABLE when it has neither of the two things that can move
    it: an open ``pending_approvals`` row (which ``mark_approval_resolved`` →
    ``_wake_waiting_workflow`` would resolve) nor a ``stall_metadata->>'wait_workflow_id'`` stamp (the
    VT-671 DBOS.send target). And the retry ladder cannot help: ``reap_stalled_manager_tasks``
    deliberately EXCLUDES ``waiting_owner`` so it can never burn an awaiting-approval task to
    dead_letter (``task_store.py:280``) — correct for approvals, fatal for this shape.

    WHY THIS IS 'critical' AND NOT ANOTHER STALL WARNING. Because ``waiting_owner`` is in
    ``TASK_ACTIVE``, ``queue_promotion.promote_next_queued_task`` refuses to advance anything while the
    parked task sits there — and the promoter is only ever invoked from a TERMINAL task's workflow
    tail, which this task will never reach. **So every later objective for that tenant queues behind it
    forever.** The Manager does not degrade, it ends, and no seam recovers from it unaided. The alert IS
    the recovery path.

    Measured on deployed dev 2026-08-14: 4 of 7 ``waiting_owner`` tasks were in this state, and 1
    tenant already had a ``queued`` task stranded behind one.

    Detect-and-alert only — it mutates NOTHING. Un-wedging means deciding what happens to the parked
    objective (cancel it? escalate it? ask the owner something answerable?), which is VT-755's other
    scope items, not a sweep's call. Best-effort and never raises, like every detector here.
    """
    try:
        with _service_pool(pool).connection() as conn:
            rows = conn.execute(
                "SELECT t.id, t.tenant_id, t.updated_at, "
                "       (SELECT count(*) FROM manager_tasks q "
                "          WHERE q.tenant_id = t.tenant_id AND q.status = 'queued') AS queued_behind "
                "FROM manager_tasks t "
                "WHERE t.status = 'waiting_owner' "
                "  AND t.updated_at < now() - make_interval(hours => %s) "
                "  AND t.stall_metadata->>'wait_workflow_id' IS NULL "
                "  AND NOT EXISTS (SELECT 1 FROM pending_approvals p "
                "                  WHERE p.tenant_id = t.tenant_id AND p.resolved_at IS NULL) "
                "LIMIT 200",
                (age_hours,),
            ).fetchall()
        if rows:
            _alert_wedged_tenants(rows)
        return len(rows)
    except Exception:  # noqa: BLE001 — a detector must never block boot or a scheduler tick
        logger.warning("VT-755 wedged-tenant detector failed (best-effort)", exc_info=True)
        return 0


def _alert_wedged_tenants(rows: Any) -> None:
    """One ``wedged_tenant`` alert per unreachable park (fail-soft).

    The message names the CONSEQUENCE, not the symptom: an operator reading "task parked" would triage
    it as one stuck job, when in fact nothing that tenant asks will run again until a human intervenes.
    """
    try:
        from uuid import UUID

        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for
    except Exception:  # noqa: BLE001
        logger.warning("VT-755 wedged_tenant alert import failed (fail-soft)", exc_info=True)
        return
    for row in rows:
        try:
            tid = row["tenant_id"] if isinstance(row, dict) else row[1]
            task_id = row["id"] if isinstance(row, dict) else row[0]
            queued = int((row["queued_behind"] if isinstance(row, dict) else row[3]) or 0)
            dispatch_alert(Trigger(
                tenant_id=tid if isinstance(tid, UUID) else UUID(str(tid)),
                trigger_kind="wedged_tenant",
                severity=severity_for("wedged_tenant"),
                message_text=(
                    f"WEDGED: task {task_id} is parked 'waiting_owner' with NO open approval and NO "
                    f"wake stamp, so nothing can wake it and the retry ladder excludes it. "
                    f"{queued} later objective(s) are already queued behind it and CANNOT run — "
                    "'waiting_owner' counts as active, so the queue promoter is blocked until this "
                    "task reaches terminal, which it never will. This tenant's Manager is dead until "
                    "someone cancels or escalates that task. Nothing sent; nothing lost yet."
                ),
                payload={
                    "task_id": str(task_id),
                    "queued_behind": queued,
                    "detected_by": "vt755_wedged_tenant_detector",
                },
            ))
        except Exception:  # noqa: BLE001
            logger.warning("VT-755 wedged_tenant alert dispatch failed (fail-soft)", exc_info=True)


__all__ = [
    "reap_orphan_runs",
    "reap_stalled_manager_tasks",
    "reap_crashed_campaigns",
    "detect_silent_terminal_runs",
    "detect_wedged_tenants",
]
