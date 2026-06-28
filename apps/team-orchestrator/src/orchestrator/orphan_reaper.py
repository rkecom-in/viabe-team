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


__all__ = ["reap_orphan_runs"]
