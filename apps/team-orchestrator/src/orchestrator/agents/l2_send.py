"""VT-418 — the L2 owner-approve→send DRIVER (the missing selector + invoker).

The connector-audit dead end this module fixes (plan §0): approval flips a batch to
``'approved'`` in ``approval_glue.apply_agent_decision`` (``customer_send's stack takes over``
— aspirational), but NOTHING selected an ``approved`` batch and called ``agent_send_draft``.
The batch just SAT in ``approved``. ``agent_send_draft`` already accepts ``approved`` at its
Gate-1 batch-state check and CAS-flips ``approved→sending`` — the send function is complete and
proven. What was missing is the thing that CALLS it on an approved batch.

VT-418 = that driver, the L2 sibling of ``l3_hold`` (plan §1):

  ``l2_send_workflow(tenant_id, batch_id)`` — the durable @DBOS.workflow, keyed
      ``l2_send_{batch_id}`` (exactly-once START — a redelivered owner-reply / double
      resolution physically cannot spawn two send drivers for one batch). Runs ONE
      checkpointed send step (no poll loop — unlike L3 there is NO delivery anchor to wait on;
      an owner-approved batch sends NOW). Mirrors ``l3_hold._hold_send_step_body``.
  ``start_l2_send(tenant_id, batch_id)`` — ``DBOS.start_workflow`` under
      ``SetWorkflowID(l2_send_{batch_id})``. Started AFTER the approval-resolution transaction
      COMMITS (the runner ``agent_customer_send`` post-commit seam — mirrors the L3 arm's
      start-after-flip, ``sales_recovery_executor.py``). Start-after-commit is mandatory:
      starting inside the resolve txn would orphan a workflow if the resolve rolled back.
  ``register_l2_send()`` — idempotent workflow registration; called from main.py lifespan
      BEFORE ``launch_dbos()`` (the register-before-launch contract — the workflow must be in
      the DBOS registry when launch computes the app_version hash, so the executor's
      ``start_l2_send`` + DBOS recovery of a parked/crashed run resolve).
  ``l2_approved_send_sweep_scheduled`` / ``run_l2_approved_send_sweep_body`` — the
      RECONCILER (recovery-only, plan §1B). A @DBOS.scheduled job that heals the residual where
      the post-commit ``start_l2_send`` never ran (the process died between the resolution
      commit and the start call — the exact analog of the L3 "armed-but-hold-unstarted"
      residual). It selects batches STUCK in ``approved`` (older than a small grace) and calls
      ``start_l2_send`` for each — idempotent on the workflow-id (a no-op if the primary already
      started it). Best-effort + fail-soft like every other scheduled sweep.

IDEMPOTENCY (LOAD-BEARING, money-send — plan §3): the double-send guarantee is ALREADY enforced
inside ``agent_send_draft`` → ``send_whatsapp_template``. The driver REUSES the existing
``agent:{draft_id}`` dedup in ``send_idempotency_keys`` and adds NOTHING new. ``'sent'`` is a
permanent hit (24h TTL → a delivered draft NEVER re-sends); ``'error'`` is DELIBERATELY excluded
from the hit set (VT-387/410 — a transiently-failed send is eligible to re-send on a later run).
The workflow's exactly-once START + the per-draft ledger dedup together guarantee: for ANY draft,
AT MOST ONE real Twilio send across any number of driver invocations (primary re-run, sweep
re-select, mid-send restart, redelivered owner-reply). The driver re-implements NO gate.

Scope boundary (plan §7): NO gate change (opt-out/consent/caps/recency stay in
``agent_send_draft``), NO new template/SID/twilio change, NO new migration/table/column (idempotency
reuses ``send_idempotency_keys`` + ``agent:{draft_id}``), NO C2 enablement (VT-396 owns the dev-only
never-main consent version), NO L3 change (``l3_hold`` untouched; ``l2_send`` is a sibling).

CL-390: logs carry tenant/batch/draft ids + counters + statuses only — never owner_feedback,
never draft params, never a phone.

``dbos`` is imported lazily (only the DBOS-workflow / start callers reach it) so this module stays
importable dep-less — the ``l3_hold`` / run_control precedent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

# The reconciler-sweep cadence + staleness grace (plan §1B). Off-peak, recovery-only. A batch
# must sit in 'approved' past the grace before the sweep re-drives it — the primary post-commit
# start gets first crack; the sweep only heals the crash-window residual.
L2_APPROVED_SEND_SWEEP_CRON = "*/15 * * * *"  # every 15 minutes
_STUCK_APPROVED_GRACE = "10 minutes"  # interval literal — only re-drive batches older than this


def _col(row: Any, key: str, idx: int) -> Any:
    return row[key] if isinstance(row, dict) else row[idx]


# ---------------------------------------------------------------------------
# The SEND STEP — directly callable (a stable-qualname @DBOS.step at runtime).
# Mirrors l3_hold._hold_send_step_body MINUS the hold/anchor/silence machinery.
# ---------------------------------------------------------------------------


def _l2_send_step_body(tenant_id: str, batch_id: str) -> dict[str, Any]:
    """The owner-approve send leg (checkpointed). Re-confirms the batch is STILL ``approved``
    under the tenant connection (a concurrent cancel/edit may have left it — send nothing then),
    enumerates its ``drafted`` drafts, and calls ``agent_send_draft(autonomy_level='L2')`` per
    draft. EVERY gate re-runs inside ``agent_send_draft`` (registry/opt-out/consent/caps/
    idempotency) — the driver re-implements none of it. Returns per-status counters (IDs-only).

    Restart safety: on a DBOS recovery the step re-runs; the per-draft loop selects only
    ``status='drafted'`` drafts (already-sent ones are skipped at SELECTION time) AND the
    ``agent:{draft_id}`` ledger hit short-circuits any already-delivered draft inside
    ``send_whatsapp_template`` — so re-running cannot double-send (plan §3)."""
    from orchestrator.agents.customer_send import agent_send_draft

    with tenant_connection(tenant_id) as conn:
        # Re-confirm the batch is STILL drivable — 'approved' (not yet started) OR 'sending'
        # (mid-batch: the first draft's agent_send_draft CAS-flipped approved→sending; a DBOS
        # recovery re-run after a mid-send crash MUST be able to continue the remaining 'drafted'
        # drafts, so 'sending' is drivable too — it mirrors agent_send_draft's Gate-1
        # _ok_batch_states=('approved','sending') for L2). A batch left cancelled/edit_requested/
        # rejected/sent by a racing resolution is NOT drivable → send nothing (the gate stack would
        # fail-closed anyway, but skipping the loop is cleaner + cheaper).
        still = conn.execute(
            "SELECT 1 FROM agent_draft_batches WHERE tenant_id = %s AND id = %s "
            "AND status IN ('approved', 'sending')",
            (tenant_id, batch_id),
        ).fetchone()
        if still is None:
            logger.info(
                "l2_send: batch not drivable (not approved/sending) tenant=%s batch=%s — no-op",
                tenant_id, batch_id,
            )
            return {"sent": 0, "skipped": 0, "failed": 0, "raced_out": 1}
        draft_ids = [
            str(_col(r, "did", 0))
            for r in conn.execute(
                "SELECT id::text AS did FROM agent_drafts "
                "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted'",
                (tenant_id, batch_id),
            ).fetchall()
        ]
    counters = {"sent": 0, "skipped": 0, "failed": 0}
    for did in draft_ids:
        # Each call opens its own tenant_connection (the gate stack's contract). autonomy_level='L2'
        # is the unlocked owner-approved arm of agent_send_draft (CAS approved→sending, send,
        # draft→sent, contacts ledger row, batch sending→sent via _finalize_batch_if_terminal).
        result = agent_send_draft(tenant_id, did, autonomy_level="L2")
        if result.status in ("sent", "already_sent"):
            counters["sent"] += 1
        elif result.status == "skipped":
            counters["skipped"] += 1
        else:
            counters["failed"] += 1
    logger.info(
        "l2_send: owner-approve send tenant=%s batch=%s sent=%d skipped=%d failed=%d",
        tenant_id, batch_id, counters["sent"], counters["skipped"], counters["failed"],
    )
    return counters


# The lazily-decorated @DBOS.step (a DISTINCT name from the _l2_send_step wrapper below, so the
# wrapper's re-dispatch does NOT recurse — the l3_hold idiom reuses one name, which only works
# because its wrapper is never reached in-process; we keep them separate to be in-process-safe).
_l2_send_step_decorated: Any | None = None


def _ensure_l2_send_step() -> None:
    """Lazily decorate the send leg as @DBOS.step (a stable qualname for recovery — the
    ``l3_hold._ensure_hold_steps`` idiom)."""
    from dbos import DBOS

    global _l2_send_step_decorated
    if _l2_send_step_decorated is None:
        _l2_send_step_decorated = DBOS.step()(_l2_send_step_body)


def _l2_send_step(tenant_id: str, batch_id: str) -> dict[str, Any]:
    """Dispatch the send leg through its @DBOS.step decoration (checkpointed for recovery)."""
    _ensure_l2_send_step()
    assert _l2_send_step_decorated is not None
    return _l2_send_step_decorated(tenant_id, batch_id)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# The DURABLE WORKFLOW — one checkpointed send step, no poll loop.
# ---------------------------------------------------------------------------


def l2_send_workflow(tenant_id: str, batch_id: str) -> dict[str, Any]:
    """The durable L2 send (plan §1A). Unlike ``l3_hold_workflow`` there is NO delivery anchor to
    park on — an owner-approved batch sends NOW — so this runs ONE checkpointed send step and
    returns its counters. On a crash mid-send, DBOS workflow recovery re-runs the step; the send
    is idempotent (plan §3) so re-running cannot double-send.

    ``dbos`` imports lazily so the module stays importable dep-less (the run_control precedent)."""
    counters = _l2_send_step(tenant_id, batch_id)
    return {"tenant_id": tenant_id, "batch_id": batch_id, "outcome": "sent", **counters}


# ---------------------------------------------------------------------------
# Registration + START (the house register-before-launch pattern; idempotent).
# ---------------------------------------------------------------------------

_registered = False


def register_l2_send() -> None:
    """Apply ``@DBOS.workflow`` to :func:`l2_send_workflow`. Call from main.py lifespan BEFORE
    ``launch_dbos()`` (the ``register_l3_hold`` precedent — workflow registration must be in the
    registry when launch computes the app_version hash, so ``start_l2_send`` + DBOS recovery of a
    parked/crashed run resolve). Idempotent."""
    from dbos import DBOS

    global _registered
    if _registered:
        return
    DBOS.workflow()(l2_send_workflow)
    _registered = True


def start_l2_send(tenant_id: str, batch_id: str) -> None:
    """Start the durable send workflow for an approved batch (idempotent on the workflow_id —
    ``DBOS.start_workflow`` no-ops on a known id). Keyed on the batch so a redelivered approval
    resolution / a sweep re-select cannot spawn two send drivers for one batch. Direct copy of
    ``l3_hold.start_l3_hold``."""
    from dbos import DBOS, SetWorkflowID

    workflow_id = f"l2_send_{batch_id}"
    with SetWorkflowID(workflow_id):
        DBOS.start_workflow(l2_send_workflow, tenant_id, batch_id)


def start_l2_send_for_resolved_approval(tenant_id: str, approval_id: str) -> str | None:
    """The runner POST-COMMIT arm seam (plan §1A). Called from
    ``runner._maybe_resume_owner_approval`` AFTER the approval-resolution transaction has committed
    the batch flip to ``'approved'``, on an ``agent_customer_send`` approval that resolved
    ``approved``. Looks up the approval's linked batch, confirms it is NOW ``'approved'`` (the flip
    committed; a stale / non-approved resolution is a safe no-op — never starts a send over a batch
    that did NOT reach approved), and starts the durable send workflow. Returns the started batch_id
    (else None).

    Start-after-commit is mandatory (plan §1A): starting inside the resolve txn would orphan a
    workflow if the resolve rolled back. ``start_l2_send`` is idempotent on the workflow-id, so a
    redelivered owner-reply cannot spawn two drivers; if THIS start never runs (process death in the
    commit→start window) the reconciler sweep (§1B) heals it — the L3 ``armed-but-hold-unstarted``
    recovery posture. Errors are logged + swallowed (the batch is durably ``approved`` and the sweep
    is the recovery seam) — an arm error must NEVER fail the owner-reply path."""
    try:
        from orchestrator.db.wrappers import PendingApprovalsWrapper

        # Wrapper-scoped composite read (VT-72): the pending_approvals×agent_draft_batches join lives
        # in the wrapper, not inline here.
        batch_id = PendingApprovalsWrapper().approved_batch_for_send_approval(tenant_id, approval_id)
        if batch_id is None:
            # The batch did not reach 'approved' (stale resolution / non-resolvable batch /
            # not an agent_customer_send approval) — nothing to drive. Safe no-op.
            return None
        start_l2_send(tenant_id, batch_id)
        logger.info(
            "l2_send: started post-approval send tenant=%s batch=%s approval=%s",
            tenant_id, batch_id, approval_id,
        )
        return batch_id
    except Exception:  # noqa: BLE001 — arm error must not fail the owner reply; sweep recovers it
        logger.exception(
            "l2_send: post-approval start failed tenant=%s approval=%s "
            "(batch durably 'approved' if flipped; reconciler sweep is the recovery seam)",
            tenant_id, approval_id,
        )
        return None


# ---------------------------------------------------------------------------
# RECONCILER SWEEP (recovery-only, plan §1B) — heals the crash-between-commit-
# and-start residual. Idempotent on the workflow-id (a no-op if the primary
# post-commit start already fired). Best-effort + fail-soft.
# ---------------------------------------------------------------------------


def _scan_stuck_approved_l2_batches(now: datetime) -> list[dict[str, str]]:
    """Return batches STUCK in ``'approved'`` past the staleness grace, workspace-wide
    (service-role read, no GUC) — the cross-tenant scan pattern of
    ``scheduled_triggers._scan_timed_out_approvals``. An ``'approved'`` batch is BY DEFINITION L2:
    the L3 arm routes its batches to ``auto_send_pending``, never ``approved`` — so
    ``status='approved'`` alone excludes L3 (plan §1 selection query). The per-batch
    ``start_l2_send`` below re-scopes the tenant GUC inside the workflow's own step.

    The grace filter (``updated_at`` older than ``_STUCK_APPROVED_GRACE``) gives the primary
    post-commit start first crack — the sweep only re-drives a batch the primary missed.

    Scope (plan §1B): the reconciler heals the commit→start residual ONLY (batches stuck in
    ``approved`` that never had their workflow started). A batch stranded in ``sending`` (a workflow
    that started, CAS-flipped, then crashed mid-send) is NOT this sweep's job — it is healed by DBOS
    workflow recovery re-running the (``sending``-tolerant) send step on restart. Selecting
    ``sending`` here would risk re-driving a batch a LIVE workflow is mid-sending; the per-batch
    exactly-once ``l2_send_{batch_id}`` start + the ledger dedup make even that safe, but staying
    scoped to ``approved`` is the blessed-plan boundary."""
    from psycopg.rows import dict_row

    from orchestrator.graph import get_pool

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id::text AS batch_id, tenant_id::text AS tenant_id
            FROM agent_draft_batches
            WHERE status = 'approved'
              AND updated_at < %s - interval '""" + _STUCK_APPROVED_GRACE + """'
              AND EXISTS (
                  SELECT 1 FROM agent_drafts d
                  WHERE d.tenant_id = agent_draft_batches.tenant_id
                    AND d.batch_id = agent_draft_batches.id
                    AND d.status = 'drafted'
              )
            ORDER BY updated_at ASC
            """,
            (now,),
        )
        return [dict(row) for row in cur.fetchall()]


def run_l2_approved_send_sweep_body(now: datetime | None = None) -> list[str]:
    """Reconciler body — REAL (VT-418, plan §1B). For each batch stuck in ``approved`` past the
    grace with ``drafted`` drafts, (re)start the durable send workflow. ``start_l2_send`` is
    idempotent on the ``l2_send_{batch_id}`` workflow-id, so this is a NO-OP when the primary
    post-commit start already ran — and the per-draft ledger dedup makes a genuine re-drive
    no-double-send (plan §3). Returns the batch ids it (re)started for canary inspection.

    Callable directly with an injected ``now`` (mirrors the other sweep bodies) so the canary can
    drive a stuck-past-grace batch without waiting for the cron. Per-batch try/except: one stuck
    start must not halt the sweep (the L3 ``armed-but-hold-unstarted`` recovery posture)."""
    now = now or datetime.now(timezone.utc)
    stuck = _scan_stuck_approved_l2_batches(now)
    started: list[str] = []
    for batch in stuck:
        tenant_id = batch["tenant_id"]
        batch_id = batch["batch_id"]
        try:
            start_l2_send(tenant_id, batch_id)
            started.append(batch_id)
            logger.info(
                "l2_send: reconciler (re)started stuck-approved batch tenant=%s batch=%s",
                tenant_id, batch_id,
            )
        except Exception:  # noqa: BLE001 — one stuck start must not halt the sweep (best-effort)
            logger.exception(
                "l2_send: reconciler start failed tenant=%s batch=%s (next sweep retries)",
                tenant_id, batch_id,
            )
    return started


def l2_approved_send_sweep_scheduled(
    scheduled_time: datetime,
    actual_time: datetime,
) -> None:
    """DBOS scheduled handler — fires every 15 min (plan §1B). Heals the crash-between-commit-
    and-start residual by (re)starting the durable send workflow for batches stuck in ``approved``.
    NO LLM — deterministic selector. Best-effort: a sweep failure must not crash the scheduler."""
    try:
        run_l2_approved_send_sweep_body(now=actual_time)
    except Exception:  # noqa: BLE001 — sweep is best-effort; the next run retries
        logger.exception("VT-418 l2-approved-send reconciler sweep scheduled run failed")


__all__ = [
    "L2_APPROVED_SEND_SWEEP_CRON",
    "l2_approved_send_sweep_scheduled",
    "l2_send_workflow",
    "register_l2_send",
    "run_l2_approved_send_sweep_body",
    "start_l2_send",
    "start_l2_send_for_resolved_approval",
]
