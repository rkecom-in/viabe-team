"""VT-93 — refund execution + 30-day graceful exit.

``execute_refund(tenant_id, refund_reason)`` is the single refund execution path
(Pillar 8). Called by the day-39 auto-path (and, once VT-85 lands, by the owner's
REFUND reply). It is a PLAIN function — NOT a ``@DBOS.step`` — because it
orchestrates other steps (``apply_transition``, ``send_template_message``); a
DBOS step may not call another step. Idempotency therefore lives in the DB:
``db.refund_executions.claim_or_get`` takes a per-tenant advisory lock + an
``INSERT ... ON CONFLICT`` + ``SELECT ... FOR UPDATE``, so two concurrent calls
cannot both refund.

Ordering (phantom-refund safe): the money moves first; the phase flips to
``refunded`` ONLY after refunds + cancel succeed, and the row is frozen
``completed`` last. A crash between the transition and the freeze leaves a
recoverable refunding row (the durable signal is the refund_executions status +
the privacy_audit_log entry), not a double-refund.

Razorpay is STUBBED (NEEDS-FAZAL: live keys / VT-89). The refund amount is the
running fee total (``subscriptions.cumulative_fees_paid_paise``) as a single
call; per-payment splitting arrives with VT-89's charge ledger behind the same
seam. Owner-notification templates carry null SIDs (NEEDS-FAZAL) → the refund
still COMPLETES, with a loud audit + Fazal alert (a notification gap is not a
refund failure).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from orchestrator.billing.razorpay_refund import (
    RazorpayClient,
    RazorpayRefundError,
    default_razorpay_client,
)
from orchestrator.db import refund_executions as _ledger
from orchestrator.db import tenant_connection
from orchestrator.graph import get_pool
from orchestrator.observability.audit_log import log_privacy_event
from orchestrator.observability.log import log_event

logger = logging.getLogger(__name__)

_VALID_REASONS = ("day39_eligibility", "manual_request")


@dataclass(frozen=True)
class RefundExecution:
    """PII-free result of one refund execution."""

    tenant_id: UUID
    refund_reason: str
    status: str
    total_refund_paise: int
    partial_refund_paise: int
    completed: bool


def _idem_key(tenant_id: UUID, refund_reason: str, step: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{refund_reason}:{step}".encode()).hexdigest()


def _read_fees_and_subscription(tenant_id: UUID) -> tuple[int, str | None]:
    """Full-refund amount (running fee total) + a representative Razorpay
    subscription id to cancel. Service-role read with an explicit WHERE tenant_id
    — ``subscriptions`` predates the migration-015 app_role grant (not in the
    ALTER DEFAULT PRIVILEGES set), so a tenant_connection (SET ROLE app_role) read
    is permission-denied; day39_evaluator reads it the same privileged way.
    Phase-1: one subscription per tenant; SUM is a multi-row safety belt."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cumulative_fees_paid_paise), 0)::BIGINT AS fees, "
            "       MAX(razorpay_subscription_id) AS sub_id "
            "FROM subscriptions WHERE tenant_id = %s",
            (str(tenant_id),),
        )
        row = cur.fetchone() or {}
    return int(row.get("fees") or 0), row.get("sub_id")


def _alert_fazal(text: str) -> None:
    """Best-effort Telegram alert to the ops channel. Never raises into the
    refund path. Loop-safe: if an event loop is already running (an async caller),
    ``asyncio.run`` would raise — so the send is off-loaded to a worker thread
    rather than silently dropped (a financial alert must not vanish in async
    contexts). (DISCUSS / partial-failure escalation reuse this.)"""
    import asyncio
    import threading

    from orchestrator.alerts.clients import send_telegram

    def _run() -> None:
        try:
            asyncio.run(
                send_telegram(
                    os.environ.get("TELEGRAM_OPS_BOT_TOKEN", ""),
                    os.environ.get("TELEGRAM_OPS_CHAT_ID", ""),
                    text,
                )
            )
        except Exception:  # noqa: BLE001 — alert is best-effort, never blocks a refund
            logger.exception("refund: Fazal alert failed (best-effort)")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _run()  # no running loop — safe to asyncio.run inline
        return
    # A loop is already running; off-thread the send so asyncio.run can't raise.
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)


def _audit(tenant_id: UUID, refund_reason: str, event_type: str, payload: dict[str, Any]) -> None:
    """Append to the immutable privacy_audit_log hash-chain (BYPASSRLS conn).
    PII-free payload only (ids/amounts/status) per CL-390. This is the DURABLE
    record that survives a DSR hard-delete of the refund_executions row."""
    try:
        with get_pool().connection() as conn:
            log_privacy_event(
                conn,
                tenant_id=tenant_id,
                event_type=event_type,
                payload={"refund_reason": refund_reason, **payload},
                actor="refund_executor",
            )
    except Exception:  # noqa: BLE001 — audit append is best-effort under the sweep
        logger.exception("refund: privacy audit append failed tenant=%s", tenant_id)


def _transition_to_refunded(tenant_id: UUID, refund_reason: str) -> bool:
    """Apply the ``day39_refund_triggered`` transition (-> phase 'refunded' +
    tenants.refunded_at) iff the tenant is in an eligible phase.

    Returns True if the phase is now 'refunded' (flipped, or already there on an
    idempotent re-entry); False if the flip FAILED. The caller alerts Fazal loudly
    on False — money has moved but the phase mirror needs manual reconciliation
    (the mirror is denormalised, not the source of truth). apply_transition is a
    @DBOS.step and may fail under a direct synchronous (canary) call without a
    DBOS context — caught here, surfaced as False, never silently swallowed."""
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT phase FROM tenants WHERE id = %s", (str(tenant_id),))
            row = cur.fetchone()
        if row is None:
            return False
        current_phase = row["phase"]
        if current_phase == "refunded":
            return True  # already flipped (idempotent re-entry)
        if current_phase not in ("paid_active", "paid_at_risk", "refund_offered"):
            logger.warning(
                "refund: tenant %s phase=%s not transition-eligible; refund stands",
                tenant_id,
                current_phase,
            )
            return False
        state = new_subscriber_state(tenant_id=tenant_id, run_id=uuid4(), phase=current_phase)
        apply_transition(state, "day39_refund_triggered", {"reason": refund_reason})
        return True
    except Exception:  # noqa: BLE001
        logger.exception("refund: apply_transition failed tenant=%s", tenant_id)
        return False


def _notify_owner(tenant_id: UUID, amount_paise: int) -> bool:
    """Notify the owner of the refund. VT-349 SPLIT:
    - ``refund_processing`` — the IMMEDIATE ack is a DIRECT in-window reply to the owner's
      "REFUND" message → FREE-FORM (bilingual), best-effort, does NOT gate `pending`.
    - ``refund_completed`` — sent when the refund clears (up to 5 business days later =
      OUTSIDE the window) → stays a TEMPLATE.

    Returns True if ``refund_completed`` could not be sent (NEEDS-FAZAL SID) — the caller
    records notification_pending + alerts Fazal. A send gap NEVER fails the refund."""
    from orchestrator.owner_surface.freeform_acks import (
        ack_body,
        resolve_owner_locale,
        send_freeform_ack,
    )
    from orchestrator.owner_surface.monthly_report_pdf import money_inr
    from orchestrator.utils.twilio_send import (
        get_tenant_whatsapp_number,
        send_template_message,
    )

    # 1. refund_processing — FREE-FORM in-window ack (₹ Indian-grouped, no symbol in {amt}).
    locale = resolve_owner_locale(tenant_id)
    amt = money_inr(amount_paise).removeprefix("₹")
    body = ack_body("refund_processing", locale, amt=amt)
    send_freeform_ack(tenant_id, get_tenant_whatsapp_number(tenant_id), body)

    # 2. refund_completed — TEMPLATE (out-of-window). Only this gates `pending`. Same grouped
    # `amt` as the free-form ack so both messages read identically (₹2,499, not ₹2499).
    pending = False
    params = {"1": amt}
    try:
        result = send_template_message(tenant_id, "refund_completed", params)
    except Exception:  # noqa: BLE001 — never let a notify failure unwind a refund
        logger.exception("refund: notify send raised for refund_completed")
        pending = True
    else:
        if not result.success:
            pending = True
    return pending


def execute_refund(
    tenant_id: UUID | str,
    refund_reason: str = "day39_eligibility",
    *,
    razorpay: RazorpayClient | None = None,
    day39_evaluation_id: UUID | str | None = None,
) -> RefundExecution:
    """Execute a full refund + subscription cancel + graceful exit. Idempotent on
    (tenant_id, refund_reason). See the module docstring for ordering guarantees."""
    if refund_reason not in _VALID_REASONS:
        raise ValueError(f"invalid refund_reason: {refund_reason!r}")
    razorpay = razorpay or default_razorpay_client()
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    claim_paise, sub_id = _read_fees_and_subscription(tid)
    if claim_paise < 0:
        raise ValueError(f"refund amount cannot be negative: {claim_paise}")

    # 1. Claim the idempotency row (advisory lock + ON CONFLICT + FOR UPDATE).
    with tenant_connection(tid) as conn, conn.transaction():
        row, _created = _ledger.claim_or_get(
            conn, tid, refund_reason, claim_paise, day39_evaluation_id
        )
        status = row["status"]
        if status == "completed":
            logger.info("refund: tenant %s already completed; idempotent return", tid)
            return _to_model(row, completed=True)
        if status == "partial_failed":
            logger.warning(
                "refund: tenant %s in partial_failed; manual resolution only (no auto-retry)",
                tid,
            )
            return _to_model(row, completed=False)
        # 'pending' OR 'refunding' (crash re-entry) -> (re)enter the refunding
        # state. The authoritative amount is the CLAIMED amount stored on the row
        # (stable across re-entries — snapshot-at-decision), never a fresh read.
        if status == "pending":
            _ledger.set_status(conn, tid, refund_reason, "refunding")
        total_paise = int(row["total_refund_paise"])
        responses = row["refund_responses"] or []

    # Resume-from-step: a prior attempt may have already succeeded a step. Re-call
    # ONLY the steps not yet recorded ok (kills the double-refund on a 'refunding'
    # crash-retry). The deterministic idempotency_key is the final vendor-side
    # backstop for the concurrent-duplicate window the xact advisory lock can't
    # span (a DB txn cannot be held across an external HTTP call).
    refund_done = any(r.get("step") == "refund" and r.get("ok") for r in responses)
    cancel_done = any(r.get("step") == "cancel" and r.get("ok") for r in responses)

    # 2. Refund (external; stubbed/injected). Persist the response.
    if not refund_done:
        try:
            rr = razorpay.refund(
                amount_paise=total_paise,
                idempotency_key=_idem_key(tid, refund_reason, "refund"),
                subscription_id=sub_id,
            )
        except RazorpayRefundError as exc:
            return _halt_partial(
                tid, refund_reason, total_paise, 0, {"step": "refund", "error": str(exc)}
            )
        if not rr.ok:
            return _halt_partial(
                tid, refund_reason, total_paise, 0, {"step": "refund", "ok": False, "raw": rr.raw}
            )
        with tenant_connection(tid) as conn, conn.transaction():
            _ledger.append_response(
                conn,
                tid,
                refund_reason,
                {
                    "step": "refund",
                    "ok": True,
                    "refund_id": rr.refund_id,
                    "amount_paise": total_paise,
                },
            )

    # 3. Cancel the subscription (skip if already done on a prior attempt).
    if not cancel_done:
        try:
            cr = razorpay.cancel_subscription(
                sub_id, idempotency_key=_idem_key(tid, refund_reason, "cancel")
            )
        except RazorpayRefundError as exc:
            return _halt_cancel(
                tid, refund_reason, total_paise, {"step": "cancel", "error": str(exc)}
            )
        if not cr.ok:
            return _halt_cancel(
                tid, refund_reason, total_paise, {"step": "cancel", "ok": False, "raw": cr.raw}
            )
        with tenant_connection(tid) as conn, conn.transaction():
            _ledger.append_response(conn, tid, refund_reason, {"step": "cancel", "ok": True})

    # 4. Phase -> refunded (sets refunded_at), then freeze the row completed. A
    #    failed flip does NOT silently pass — Fazal is alerted (money moved; the
    #    denormalised phase mirror needs reconciliation).
    phase_flipped = _transition_to_refunded(tid, refund_reason)
    notification_pending = _notify_owner(tid, total_paise)
    with tenant_connection(tid) as conn, conn.transaction():
        _ledger.mark_completed(
            conn,
            tid,
            refund_reason,
            partial_refund_paise=total_paise,
            notification_pending=notification_pending,
        )

    # VT-94: audit-only release of the founding slot (stamps released_at; the counter is
    # NEVER decremented — no-reopen policy). Best-effort: a missed release leaves
    # released_at NULL but never corrupts the counter (Cowork: zero integrity risk).
    # Service-role pool — founding_tier_claims has no app_role UPDATE policy.
    try:
        from orchestrator.billing.founding_counter import release_founding_slot
        from orchestrator.graph import get_pool

        with get_pool().connection() as _fc_conn:
            release_founding_slot(_fc_conn, tid)
    except Exception:
        logger.exception("founding-slot release failed (audit-only) tenant=%s", tid)

    # 5. Durable audit + observability.
    _audit(
        tid,
        refund_reason,
        "refund_executed",
        {
            "total_refund_paise": total_paise,
            "notification_pending": notification_pending,
            "phase_flipped": phase_flipped,
        },
    )
    log_event(
        event_type="refund_executed",
        run_id=uuid4(),
        tenant_id=tid,
        severity="info",
        component="billing",
        payload={
            "tenant_id": str(tid),
            "refund_reason": refund_reason,
            "total_refund_paise": total_paise,
            "notification_pending": notification_pending,
        },
    )
    if not phase_flipped:
        _alert_fazal(
            f"VT-93 refund COMPLETED for tenant {tid} but the phase flip to 'refunded' "
            f"FAILED — reconcile the tenant phase manually (refund is done; the phase "
            f"mirror is denormalised, not the source of truth)."
        )
    if notification_pending:
        _alert_fazal(
            f"VT-93 refund COMPLETED for tenant {tid} but the owner template SID is "
            f"null (NEEDS-FAZAL: refund_processing/refund_completed). Owner not notified."
        )

    out = _ledger.get(tid, refund_reason)
    return (
        _to_model(out, completed=True)
        if out
        else RefundExecution(tid, refund_reason, "completed", total_paise, total_paise, True)
    )


def _halt_partial(
    tenant_id: UUID,
    refund_reason: str,
    total_paise: int,
    partial_paise: int,
    response: dict[str, Any],
) -> RefundExecution:
    """Refund call failed — halt, record, alert Fazal. No further calls, no
    auto-retry (manual resolution; a fresh manual_request refunds the balance)."""
    with tenant_connection(tenant_id) as conn, conn.transaction():
        _ledger.append_response(conn, tenant_id, refund_reason, response)
        _ledger.mark_partial_failed(conn, tenant_id, refund_reason, partial_paise)
    _audit(
        tenant_id,
        refund_reason,
        "refund_partial_failed",
        {"partial_refund_paise": partial_paise, "failed_step": response.get("step")},
    )
    _alert_fazal(
        f"VT-93 refund PARTIAL_FAILED tenant={tenant_id} reason={refund_reason} "
        f"step={response.get('step')} — investigate; no auto-retry."
    )
    out = _ledger.get(tenant_id, refund_reason)
    return (
        _to_model(out, completed=False)
        if out
        else RefundExecution(
            tenant_id, refund_reason, "partial_failed", total_paise, partial_paise, False
        )
    )


def _halt_cancel(
    tenant_id: UUID, refund_reason: str, total_paise: int, response: dict[str, Any]
) -> RefundExecution:
    """Refunds succeeded but subscription cancel failed — pending_subscription_cancel
    (the retry sweep picks it up). Alert Fazal."""
    with tenant_connection(tenant_id) as conn, conn.transaction():
        _ledger.append_response(conn, tenant_id, refund_reason, response)
        _ledger.set_status(conn, tenant_id, refund_reason, "pending_subscription_cancel")
    _audit(
        tenant_id,
        refund_reason,
        "refund_partial_failed",
        {"partial_refund_paise": total_paise, "failed_step": "cancel"},
    )
    _alert_fazal(
        f"VT-93 refund tenant={tenant_id}: refunds done but subscription cancel FAILED "
        f"-> pending_subscription_cancel (sweep retries)."
    )
    out = _ledger.get(tenant_id, refund_reason)
    return (
        _to_model(out, completed=False)
        if out
        else RefundExecution(
            tenant_id, refund_reason, "pending_subscription_cancel", total_paise, 0, False
        )
    )


def _to_model(row: dict[str, Any], *, completed: bool) -> RefundExecution:
    return RefundExecution(
        tenant_id=row["tenant_id"]
        if isinstance(row["tenant_id"], UUID)
        else UUID(str(row["tenant_id"])),
        refund_reason=row["refund_reason"],
        status=row["status"],
        total_refund_paise=int(row["total_refund_paise"]),
        partial_refund_paise=int(row["partial_refund_paise"]),
        completed=completed,
    )
