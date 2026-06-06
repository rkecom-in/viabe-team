"""Razorpay webhook ingress (VT-89).

team-web verifies the Razorpay HMAC (it holds the webhook secret) and forwards the
parsed event here. This endpoint is the DURABLE INBOX + the sole writer of
subscription fee state + the driver of billing phase transitions.

Q1 — financial durability (Cowork 20260605T121000Z). The dedup INSERT into
``razorpay_webhook_events`` is the COMMIT POINT: team-web returns 200 to Razorpay
ONLY after this endpoint confirms the event is persisted. If this endpoint is
unreachable, or raises BEFORE the insert commits, team-web returns non-2xx so
Razorpay RETRIES — a lost ``subscription.charged`` would silently undercount fees
and under-refund later. A redelivered ``event_id`` CONFLICTs → no re-processing, so
fees never double-count (the keystone). The fee/counter writes happen in the SAME
transaction as the dedup row, so a replay that doesn't insert also doesn't re-write.

Q3 — ``payment.captured`` fires on EVERY successful charge (recurring too, each a
distinct event_id), so we map it to the ``card_captured`` phase event ONLY when the
tenant is still in {trial, trial_extended} (apply_transition RAISES on an undefined
(phase,event) pair). An already-paid tenant's recurring captured is a phase no-op;
fees move only via ``subscription.charged`` (captured→phase-only, charged→fees-only).

Service-role only: ``razorpay_webhook_events`` is deny-all RLS; ``subscriptions``
predates the app_role grant — so every write here is the privileged pool with an
explicit ``WHERE tenant_id`` (mirrors dsr_purge / refund_executor).

NO live keys / secrets here. LIVE cutover is hard-gated by VT-93-N1 + VT-329.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from orchestrator.graph import get_pool
from orchestrator.observability.log import log_event

logger = logging.getLogger(__name__)
router = APIRouter()

# 3 consecutive payment.failed → paid_at_risk (structural; raising/lowering is Type-2).
_FAILURE_THRESHOLD = 3


class RazorpayIngressBody(BaseModel):
    """Forwarded by team-web after HMAC verification — the parsed Razorpay event."""

    event_id: str  # Razorpay's event.id — the idempotency key
    event_type: str  # e.g. payment.captured, subscription.charged, payment.failed
    payload: dict[str, Any]


def _verify_internal_secret(provided: str | None) -> bool:
    """Constant-time compare against INTERNAL_API_SECRET (Pillar 8 — no bespoke crypto)."""
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _subscription_id(payload: dict[str, Any]) -> str | None:
    """Razorpay nests the subscription under payload.subscription.entity.id (charged /
    cancelled) and links the payment via payload.payment.entity.subscription_id
    (captured / failed). Defensive: try both."""
    sub = payload.get("subscription", {}).get("entity", {}).get("id")
    if sub:
        return str(sub)
    pay = payload.get("payment", {}).get("entity", {}).get("subscription_id")
    return str(pay) if pay else None


def _amount_paise_or_none(payload: dict[str, Any]) -> int | None:
    """payload.payment.entity.amount in paise, or None if it is present-but-not-an-integer
    (a DETERMINISTIC malformation). A missing/falsy amount → 0 (the normal absent case).
    VT-330: never `int()`-raise — a non-int amount used to roll back the dedup txn → 500-loop."""
    raw = payload.get("payment", {}).get("entity", {}).get("amount")
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _amount_paise(payload: dict[str, Any]) -> int:
    """The fee amount, treating an unparseable amount as 0 (the caller guards the charged
    poison-pill case up-front; this stays raise-free for the inbox/redaction path)."""
    return _amount_paise_or_none(payload) or 0


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """PII-free routing fields ONLY for the durable inbox (CL-390 — no customer PII at
    rest). The raw Razorpay payment entity carries email/contact/card; the inbox +
    audit need only the subscription id + amount. The full event was verified at the
    HMAC boundary in team-web; it is NOT stored. amount_paise is raise-free (None on a
    non-int) so a malformed amount can't roll back the dedup INSERT (VT-330)."""
    return {
        "subscription_id": _subscription_id(payload),
        "amount_paise": _amount_paise_or_none(payload),
    }


def _alert_fazal_safe(message: str) -> None:
    """Best-effort Fazal alert for a money-path anomaly (parse-drop / amount==0 / missing
    subscription). NEVER raises — the alert must not turn a recorded event into a 500."""
    try:
        from orchestrator.billing.refund_executor import _alert_fazal

        _alert_fazal(message)
    except Exception:
        logger.exception("VT-330 Fazal alert failed")


@router.post("/api/orchestrator/razorpay-ingress")
def razorpay_ingress(
    body: RazorpayIngressBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Dedup + apply the Razorpay event. 403 bad secret. 500 on a pre-persist error
    (so team-web 5xx → Razorpay retries — Q1). Returns ``{status, action}`` once the
    event is durably recorded (status: duplicate | processed | ignored)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")

    event_id = body.event_id
    event_type = body.event_type
    payload = body.payload
    sub_id = _subscription_id(payload)

    # VT-330 poison-pill guard: a subscription.charged with a non-int amount is a
    # DETERMINISTIC malformation (it can never apply; the old int()-raise rolled the dedup
    # row back → infinite 500-retry, never deduping). Record-and-drop instead of looping.
    charged_parse_drop = (
        event_type == "subscription.charged" and _amount_paise_or_none(payload) is None
    )

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                if charged_parse_drop:
                    # COMMIT the RAW event + a dropped_parse_error marker so the event is NOT
                    # lost — recorded for MANUAL reconciliation. (A same-event_id programmatic
                    # replay dedups today; the durable replay path is VT-352, a pre-LIVE gate —
                    # do NOT rely on auto-replay before then.) NO fee action. An INFRA failure
                    # HERE still raises → 500 (transient → Razorpay retries); the parse-drop 200
                    # below is reached ONLY after this commit succeeds.
                    # The WHERE guard means a previously-PROCESSED row (a good audit record) is
                    # NEVER clobbered by a later parse-drop arriving on the same event_id.
                    # VT-352: processed_at is left NULL on a drop — the event is RECORDED but NOT
                    # APPLIED (the invariant: applied IFF processed_at set). That NULL + the
                    # dropped_parse_error marker is exactly what lets the corrected event replay
                    # past the dedup below (F1); a genuinely-applied row has processed_at set and is
                    # never re-applied. (VT-330 set processed_at=now() here, which silently made the
                    # drop terminal — un-replayable — the very gap VT-352 closes.)
                    cur.execute(
                        "INSERT INTO razorpay_webhook_events "
                        "(event_id, event_type, payload, received_at) "
                        "VALUES (%s, %s, %s, now()) "
                        "ON CONFLICT (event_id) DO UPDATE "
                        "SET payload = EXCLUDED.payload "
                        "WHERE razorpay_webhook_events.processed_at IS NULL",
                        (
                            event_id,
                            event_type,
                            Jsonb({"_status": "dropped_parse_error", "raw": payload}),
                        ),
                    )
                    # VT-352: also enqueue a durable dead-letter row (PII-free routing only — the
                    # raw is in the marker above). The queue is what an operator/sweep replays; a
                    # re-POST of the corrected event (same event_id) re-processes via F1 below and
                    # flips this row to 'replayed'. ON CONFLICT DO NOTHING keeps first_seen stable
                    # across Razorpay redeliveries of the same un-fixed event.
                    cur.execute(
                        "INSERT INTO razorpay_webhook_dead_letter "
                        "(event_id, event_type, event_payload, error_reason) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                        (
                            event_id,
                            event_type,
                            Jsonb(_safe_payload(payload)),
                            "non_int_charged_amount",
                        ),
                    )
                else:
                    # Dedup = durable inbox. A replay CONFLICTs → inserted is None → no
                    # state change (the keystone: fees never double-count). The stored
                    # payload is REDACTED to PII-free routing fields (CL-390).
                    cur.execute(
                        "INSERT INTO razorpay_webhook_events "
                        "(event_id, event_type, payload, received_at) "
                        "VALUES (%s, %s, %s, now()) "
                        "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                        (event_id, event_type, Jsonb(_safe_payload(payload))),
                    )
                    is_replay = False
                    if cur.fetchone() is None:
                        # Row already exists. VT-352 F1: a same-event_id arrival is a genuine
                        # duplicate UNLESS the existing row is an un-applied parse-drop
                        # (processed_at IS NULL AND _status='dropped_parse_error') — that is the
                        # CORRECTED event replaying past the dedup, and its fee must NOW apply
                        # (else a drop silently loses the charge forever). FOR UPDATE locks the
                        # row for the apply below; a genuinely-processed row (processed_at set) is
                        # never re-applied → no double-charge.
                        cur.execute(
                            "SELECT payload ->> '_status' AS drop_status "
                            "FROM razorpay_webhook_events "
                            "WHERE event_id = %s AND processed_at IS NULL FOR UPDATE",
                            (event_id,),
                        )
                        existing = cur.fetchone()
                        if existing is None or existing["drop_status"] != "dropped_parse_error":
                            # processed_at stays NULL on a duplicate (early return) — that NULL
                            # is the "was a replay" observability marker, not used in logic.
                            return {"status": "duplicate", "action": "noop"}
                        # F1 REPLAY: overwrite the drop marker with the corrected redacted payload,
                        # then fall through to apply + processed_at + dead-letter, ALL in this one
                        # txn. ATOMIC (Cowork sharpening): if the apply raises, the whole txn rolls
                        # back to the un-applied drop → re-replayable, never half-applied.
                        is_replay = True
                        cur.execute(
                            "UPDATE razorpay_webhook_events SET payload = %s WHERE event_id = %s",
                            (Jsonb(_safe_payload(payload)), event_id),
                        )

                    # Resolve tenant from the subscription (set at /subscribe). Unknown
                    # subscription → record the event (durable) but take no state action.
                    tenant_id = _resolve_tenant(cur, sub_id)
                    action = "ignored"
                    pending_transition: str | None = None
                    if tenant_id is not None:
                        action, pending_transition = _apply_event_sql(
                            cur, tenant_id, event_type, payload, event_id=event_id
                        )
                    cur.execute(
                        "UPDATE razorpay_webhook_events SET processed_at = now() WHERE event_id = %s",
                        (event_id,),
                    )
                    if is_replay:
                        # F1: the corrected event applied → close out its dead-letter row.
                        cur.execute(
                            "UPDATE razorpay_webhook_dead_letter "
                            "SET status = 'replayed', retry_count = retry_count + 1, "
                            "    last_retry = now() "
                            "WHERE event_id = %s",
                            (event_id,),
                        )
    except Exception:
        # INFRA/pre-persist failure → 500 so team-web 5xx → Razorpay RETRIES (Q1: never
        # silently drop a financial event into the parse-drop 200). Idempotent on retry.
        logger.exception("razorpay-ingress: persist failed event_id=%s", event_id)
        raise HTTPException(status_code=500, detail="ingress persist failed") from None

    if charged_parse_drop:
        # The raw event is durably committed above; alert + 200 so Razorpay STOPS retrying.
        _alert_fazal_safe(
            f"VT-330 razorpay parse-drop: event_id={event_id} — non-int charged amount; "
            "recorded (dropped_parse_error) for manual reconciliation."
        )
        return {"status": "dropped_parse_error", "action": "drop"}

    # Phase transition AFTER the durable txn (apply_transition is a @DBOS.step with
    # its own txn). Best-effort: the event + fee state are already committed; a flip
    # failure is logged (the phase mirror is denormalised, reconcilable).
    if tenant_id is not None and pending_transition is not None:
        _apply_phase_transition(tenant_id, pending_transition)

    log_event(
        event_type="razorpay_event_processed",
        run_id=uuid4(),
        tenant_id=UUID(tenant_id) if tenant_id else None,
        severity="info",
        component="billing",
        payload={"razorpay_event_type": event_type, "action": action},
    )
    return {"status": "processed", "action": action}


def _resolve_tenant(cur: Any, subscription_id: str | None) -> str | None:
    if not subscription_id:
        return None
    cur.execute(
        "SELECT tenant_id FROM subscriptions WHERE razorpay_subscription_id = %s LIMIT 1",
        (subscription_id,),
    )
    row = cur.fetchone()
    return str(row["tenant_id"]) if row else None


def _apply_event_sql(
    cur: Any,
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> tuple[str, str | None]:
    """Apply the SQL state change for the event (inside the dedup txn) and return
    (action, pending_phase_event). The phase transition is applied by the caller
    after commit. Fees/phase are kept separate (captured→phase-only via card_captured;
    charged→fees-only) so the first payment's money is never double-counted.

    VT-330: every subscription UPDATE guards `cur.rowcount` (a deleted subscription row →
    Fazal-alert, never a silent no-op), and a charged amount==0 alerts (a real charge is
    never 0 → under-count → under-refund)."""
    if event_type == "subscription.charged":
        # The VT-93 refund-amount writer. Reset the failure counter on a success.
        amount = _amount_paise(payload)  # parseable (the charged_parse_drop guard caught None)
        cur.execute(
            "UPDATE subscriptions "
            "SET cumulative_fees_paid_paise = cumulative_fees_paid_paise + %s, "
            "    consecutive_payment_failures = 0 "
            "WHERE tenant_id = %s",
            (amount, tenant_id),
        )
        if cur.rowcount == 0:
            _alert_fazal_safe(
                f"VT-330 razorpay: subscription.charged for tenant={tenant_id} but NO "
                f"subscription row (event_id={event_id}) — fee NOT counted."
            )
            return "subscription_missing", None
        if amount == 0:
            # A real Razorpay charge is never 0 paise → a payload/parse problem. Adding 0
            # silently under-counts cumulative_fees_paid_paise → under-refund (VT-93).
            _alert_fazal_safe(
                f"VT-330 razorpay: subscription.charged amount==0 for tenant={tenant_id} "
                f"(event_id={event_id}) — under-count/under-refund risk."
            )
        return "fees_incremented", None

    if event_type == "payment.captured":
        # Reset failures; convert trial→paid ONLY if still in trial (Q3 — recurring
        # captured on an already-paid tenant is a phase no-op).
        cur.execute(
            "UPDATE subscriptions SET consecutive_payment_failures = 0 WHERE tenant_id = %s",
            (tenant_id,),
        )
        if cur.rowcount == 0:
            _alert_fazal_safe(
                f"VT-330 razorpay: payment.captured for tenant={tenant_id} but NO "
                f"subscription row (event_id={event_id})."
            )
            return "subscription_missing", None
        cur.execute("SELECT phase FROM tenants WHERE id = %s", (tenant_id,))
        row = cur.fetchone()
        phase = row["phase"] if row else None
        if phase in ("trial", "trial_extended"):
            return "converting_to_paid", "card_captured"
        return "captured_noop", None  # already paid — recurring charge, no transition

    if event_type == "payment.failed":
        cur.execute(
            "UPDATE subscriptions "
            "SET consecutive_payment_failures = consecutive_payment_failures + 1 "
            "WHERE tenant_id = %s "
            "RETURNING consecutive_payment_failures",
            (tenant_id,),
        )
        row = cur.fetchone()
        if row is None:
            # RETURNING is empty → the subscription row was deleted (race). Alert, don't
            # silently treat as 0 failures (which would never escalate a real failure run).
            _alert_fazal_safe(
                f"VT-330 razorpay: payment.failed for tenant={tenant_id} but NO "
                f"subscription row (event_id={event_id})."
            )
            return "subscription_missing", None
        count = int(row["consecutive_payment_failures"])
        if count >= _FAILURE_THRESHOLD:
            return "payment_failed_threshold", "payment_failed"
        return "payment_failed_counted", None

    if event_type == "subscription.cancelled":
        return "cancelling", "cancellation_requested"

    return "ignored", None  # unhandled event type — recorded, no action


def _apply_phase_transition(tenant_id: str, event: str) -> None:
    """Apply a billing phase transition (best-effort; the durable event + fee state
    are already committed). apply_transition is a @DBOS.step and may fail outside a
    DBOS context (canary) or on an ineligible phase — log + continue.

    The phase is RE-READ here (authoritative): if a concurrent event already moved the
    phase, apply_transition._resolve RAISES on the now-undefined (phase,event) pair and
    we no-op — never a wrong transition (the TOCTOU is safe-by-raise). The payment
    events (card_captured / payment_failed / cancellation_requested) depend ONLY on
    phase, not on trial_extension_count / paid_conversion_at, so the fresh
    new_subscriber_state is correct. A flip that fails after the fees commit leaves the
    denormalised phase mirror stale (reconcilable from phase_transitions; a
    phase-reconcile sweep is a follow-up)."""
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition

    try:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT phase FROM tenants WHERE id = %s", (tenant_id,))
            row = cur.fetchone()
        if row is None:
            return
        state = new_subscriber_state(tenant_id=UUID(tenant_id), run_id=uuid4(), phase=row["phase"])
        apply_transition(state, event, {"reason": f"razorpay:{event}"})
    except Exception:
        logger.exception(
            "razorpay-ingress: phase transition %s failed tenant=%s "
            "(event+fees already committed; mirror reconcilable)",
            event,
            tenant_id,
        )
