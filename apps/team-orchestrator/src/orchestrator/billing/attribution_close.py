"""Attribution-close aggregator (VT-175).

Sums per-campaign attribution rows + finalises the campaign row + emits
a single ``attribution_closed`` ``pipeline_log`` event. Pure SQL + a
Python wrapper.

**ZERO LLM invocations.** This module is in the scan scope of
``gate-no-llm-in-deterministic-triggers`` CI gate (Pillar 1).

Idempotency
-----------
Atomic per-row: the UPDATE uses ``WHERE attribution_closed_at IS NULL
RETURNING …``. Two simultaneous closers both read NULL, but only the
first UPDATE returns a row; the second observes 0 rows updated and
short-circuits with ``already_closed=True``. No advisory lock needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from orchestrator.billing.types import AttributionCloseResult
from orchestrator.graph import get_pool
from orchestrator.observability.log import log_event


def close_attribution(campaign_id: UUID | str) -> AttributionCloseResult:
    """Close attribution for ``campaign_id``. Service-role connection.

    Atomic + idempotent. Emits exactly one ``attribution_closed`` event
    per campaign (the call that wins the UPDATE race emits; subsequent
    callers short-circuit).
    """
    cid = str(campaign_id)

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # 1. Look up the campaign's tenant_id + current closed-at state.
        cur.execute(
            "SELECT tenant_id, attribution_closed_at, total_arrr_paise "
            "FROM campaigns WHERE id = %s",
            (cid,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"campaign {cid} not found")
        tenant_id = row["tenant_id"]
        already_closed_at = row["attribution_closed_at"]
        prior_total = row["total_arrr_paise"]

        if already_closed_at is not None:
            return AttributionCloseResult(
                campaign_id=UUID(cid),
                total_arrr_paise=int(prior_total or 0),
                closed_at=already_closed_at,
                already_closed=True,
                attribution_row_count=_count_rows(cur, cid),
            )

        # 2. Aggregate. SUM returns NULL when no rows — coalesce to 0.
        cur.execute(
            "SELECT COALESCE(SUM(attributed_paise), 0)::BIGINT AS total, "
            "       COUNT(*) AS n "
            "  FROM attributions WHERE campaign_id = %s",
            (cid,),
        )
        agg = cur.fetchone() or {}
        total_paise = int(agg.get("total") or 0)
        row_count = int(agg.get("n") or 0)

        # 3. Atomic UPDATE — race-safe. Only first writer flips
        # attribution_closed_at; second observes 0 rows updated.
        now = datetime.now(timezone.utc)
        cur.execute(
            "UPDATE campaigns SET "
            "  total_arrr_paise      = %s, "
            "  attribution_closed_at = %s "
            "WHERE id = %s AND attribution_closed_at IS NULL "
            "RETURNING attribution_closed_at",
            (total_paise, now, cid),
        )
        update_row = cur.fetchone()
        if update_row is None:
            # Lost the race. Re-read the campaign to return the winner's data.
            cur.execute(
                "SELECT attribution_closed_at, total_arrr_paise "
                "FROM campaigns WHERE id = %s",
                (cid,),
            )
            winner = cur.fetchone() or {}
            return AttributionCloseResult(
                campaign_id=UUID(cid),
                total_arrr_paise=int(winner.get("total_arrr_paise") or 0),
                closed_at=winner.get("attribution_closed_at") or now,
                already_closed=True,
                attribution_row_count=row_count,
            )

    # 4. Emit the canonical completion event — only the winning writer reaches here.
    log_event(
        event_type="attribution_closed",
        run_id=uuid4(),
        tenant_id=tenant_id,
        severity="info",
        component="billing",
        payload={
            "campaign_id": cid,
            "tenant_id": str(tenant_id),
            "total_arrr_paise": total_paise,
            "attribution_row_count": row_count,
            "closed_at_utc": now.isoformat(),
        },
    )

    return AttributionCloseResult(
        campaign_id=UUID(cid),
        total_arrr_paise=total_paise,
        closed_at=now,
        already_closed=False,
        attribution_row_count=row_count,
    )


def _count_rows(cur, campaign_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS n FROM attributions WHERE campaign_id = %s",
        (campaign_id,),
    )
    r = cur.fetchone() or {}
    return int(r.get("n") or 0)


__all__ = ["close_attribution"]
