"""Day-39 ARRR-vs-fees evaluator (VT-175).

Pure deterministic SQL aggregation + Python comparison. Returns one
:class:`Day39Verdict` per tenant. Idempotent — re-runs replay the
previously-emitted verdict from ``pipeline_log`` instead of
re-evaluating.

**ZERO LLM invocations.** This module is in the scan scope of
``gate-no-llm-in-deterministic-triggers`` CI gate (Pillar 1).

Verdict rule
------------
``ARRR >= 2 * cumulative_fees_paise`` → ``continue``; else
``refund_triggered``. Tenants whose ``paid_conversion_at + 39 days`` is
in the future return ``not_eligible`` with zero pipeline_log emission.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from orchestrator.billing.types import Day39Verdict, Day39VerdictKind
from orchestrator.graph import get_pool
from orchestrator.observability.log import log_event


DAY39_WINDOW = timedelta(days=39)
FEES_PER_ARRR_MULTIPLIER = 2


def evaluate_day39(tenant_id: UUID | str) -> Day39Verdict:
    """Evaluate day-39 ARRR-vs-fees verdict for ``tenant_id``.

    Returns :class:`Day39Verdict`. Service-role connection (cross-table
    aggregation — `attributions` + `subscriptions` — under privileged
    role since the deterministic evaluator is workspace-owned).

    Idempotency: if a prior `day39_continue` or `day39_refund_triggered`
    event already exists for this tenant within the eligibility window,
    return the prior verdict with ``already_decided=True`` and do NOT
    re-emit. The prior decision is the binding one (Pillar 1 — the
    deterministic outcome doesn't depend on when you re-run it).
    """
    tid = str(tenant_id)
    now = datetime.now(timezone.utc)

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # 1. Eligibility — must have paid_conversion_at + 39d <= now().
        cur.execute(
            "SELECT paid_conversion_at FROM tenants WHERE id = %s",
            (tid,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"tenant {tid} not found")
        paid_at = row.get("paid_conversion_at")
        if paid_at is None or paid_at + DAY39_WINDOW > now:
            return Day39Verdict(
                tenant_id=UUID(tid),
                verdict="not_eligible",
                arrr_paise=0,
                cumulative_fees_paise=0,
                decided_at=now,
                already_decided=False,
            )

        # 2. Idempotency — look up a prior decision event for this tenant.
        prior = _prior_verdict(cur, tid)
        if prior is not None:
            return prior

        # 3. Aggregate ARRR — sum attributed_paise from attributions where
        # the attribution_at falls within the day-39 window.
        cur.execute(
            "SELECT COALESCE(SUM(attributed_paise), 0)::BIGINT AS arrr "
            "FROM attributions "
            "WHERE tenant_id = %s "
            "  AND attribution_at >= %s "
            "  AND attribution_at < %s",
            (tid, paid_at, paid_at + DAY39_WINDOW),
        )
        arrr_row = cur.fetchone() or {}
        arrr_paise = int(arrr_row.get("arrr") or 0)

        # 4. Aggregate cumulative fees from subscriptions table. Schema:
        # 003_subscriptions.sql exposes `cumulative_fees_paid_paise` BIGINT
        # — a running total maintained by the Razorpay webhook handler.
        # Phase-1 invariant: one subscription per tenant; SUM across rows
        # is a safety belt for the multi-subscription case. Coalesce to 0
        # when no subscription rows yet (founding cohort before first
        # webhook).
        cur.execute(
            "SELECT COALESCE(SUM(cumulative_fees_paid_paise), 0)::BIGINT AS fees "
            "FROM subscriptions WHERE tenant_id = %s",
            (tid,),
        )
        fees_row = cur.fetchone() or {}
        fees_paise = int(fees_row.get("fees") or 0)

    verdict_kind: Day39VerdictKind = (
        "continue"
        if arrr_paise >= FEES_PER_ARRR_MULTIPLIER * fees_paise
        else "refund_triggered"
    )

    # 5. Emit the verdict event. Outside the connection block — log_event
    # opens its own connection (fire-and-forget) per VT-102's writer
    # contract.
    event_type = (
        "day39_continue" if verdict_kind == "continue" else "day39_refund_triggered"
    )
    log_event(
        event_type=event_type,
        run_id=uuid4(),
        tenant_id=UUID(tid),
        severity="info",
        component="billing",
        payload={
            "tenant_id": tid,
            "verdict": verdict_kind,
            "arrr_paise": arrr_paise,
            "cumulative_fees_paise": fees_paise,
            "multiplier_threshold": FEES_PER_ARRR_MULTIPLIER,
            "decided_at_utc": now.isoformat(),
            "paid_conversion_at_utc": paid_at.astimezone(timezone.utc).isoformat(),
        },
    )

    return Day39Verdict(
        tenant_id=UUID(tid),
        verdict=verdict_kind,
        arrr_paise=arrr_paise,
        cumulative_fees_paise=fees_paise,
        decided_at=now,
        already_decided=False,
    )


def _prior_verdict(cur, tid: str) -> Day39Verdict | None:
    """Replay a prior day-39 decision if one already landed for this tenant."""
    cur.execute(
        "SELECT event_type, payload, created_at "
        "FROM pipeline_log "
        "WHERE tenant_id = %s "
        "  AND event_type IN ('day39_continue', 'day39_refund_triggered') "
        "ORDER BY created_at ASC "
        "LIMIT 1",
        (tid,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    payload = row["payload"] or {}
    verdict_kind: Day39VerdictKind = (
        "continue" if row["event_type"] == "day39_continue" else "refund_triggered"
    )
    return Day39Verdict(
        tenant_id=UUID(tid),
        verdict=verdict_kind,
        arrr_paise=int(payload.get("arrr_paise") or 0),
        cumulative_fees_paise=int(payload.get("cumulative_fees_paise") or 0),
        decided_at=row["created_at"],
        already_decided=True,
    )


__all__ = ["DAY39_WINDOW", "FEES_PER_ARRR_MULTIPLIER", "evaluate_day39"]
