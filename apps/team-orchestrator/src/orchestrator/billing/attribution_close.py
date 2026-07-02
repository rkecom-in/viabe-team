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

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from orchestrator.billing.attribution_writer import build_campaign_attributions
from orchestrator.billing.types import AttributionCloseResult
from orchestrator.graph import get_pool
from orchestrator.observability.log import log_event

logger = logging.getLogger(__name__)


def close_attribution(campaign_id: UUID | str) -> AttributionCloseResult:
    """Close attribution for ``campaign_id``. Service-role connection.

    Atomic + idempotent. Emits exactly one ``attribution_closed`` event
    per campaign (the call that wins the UPDATE race emits; subsequent
    callers short-circuit).
    """
    cid = str(campaign_id)

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # 1. Look up the campaign's tenant_id, close state, run linkage (for the
        # VT-563 back-annotation), attribution window + plan (for the baseline).
        cur.execute(
            "SELECT tenant_id, attribution_closed_at, total_arrr_paise, "
            "       run_id, attribution_close_at, plan_json "
            "FROM campaigns WHERE id = %s",
            (cid,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"campaign {cid} not found")
        tenant_id = row["tenant_id"]
        already_closed_at = row["attribution_closed_at"]
        prior_total = row["total_arrr_paise"]
        run_id = row["run_id"]
        close_at = row["attribution_close_at"]
        baseline_paise = _baseline_paise(row["plan_json"])

        if already_closed_at is not None:
            return AttributionCloseResult(
                campaign_id=UUID(cid),
                total_arrr_paise=int(prior_total or 0),
                closed_at=already_closed_at,
                already_closed=True,
                attribution_row_count=_count_rows(cur, cid),
            )

        # 2. Atomic close — race-safe. The claim UPDATE flips attribution_closed_at
        # WHERE it IS NULL; only the first writer's UPDATE returns a row, so ONLY
        # the winner produces + aggregates + back-annotates. The whole close body
        # thus runs EXACTLY ONCE per campaign — the idempotency guard that also
        # makes the VT-563 producer exactly-once (no per-payment double-count).
        now = datetime.now(timezone.utc)
        # VT-65 PR-2: the close + the attribution_created emit (campaign arrr_paise
        # aggregate → Campaign node) are atomic in one txn; only the race-winner emits.
        with conn.transaction():
            cur.execute(
                "UPDATE campaigns SET attribution_closed_at = %s "
                "WHERE id = %s AND attribution_closed_at IS NULL "
                "RETURNING id",
                (now, cid),
            )
            won = cur.fetchone() is not None
            if won:
                # VT-563: PRODUCE the attributions rows for this campaign before
                # aggregating — recipients' payments in the attribution window. If
                # attribution_close_at is unset, fall back to `now` for the window.
                build_campaign_attributions(cur, tenant_id, cid, close_at or now)

                # Aggregate produced + any pre-existing rows. SUM is NULL-safe.
                cur.execute(
                    "SELECT COALESCE(SUM(attributed_paise), 0)::BIGINT AS total, "
                    "       COUNT(*) AS n "
                    "  FROM attributions WHERE campaign_id = %s AND tenant_id = %s",
                    (cid, str(tenant_id)),
                )
                agg = cur.fetchone() or {}
                total_paise = int(agg.get("total") or 0)
                row_count = int(agg.get("n") or 0)

                cur.execute(
                    "UPDATE campaigns SET total_arrr_paise = %s WHERE id = %s",
                    (total_paise, cid),
                )

                from orchestrator.knowledge.kg_emit import emit_kg_event
                from orchestrator.knowledge.kg_vocab import KgEventType

                emit_kg_event(conn, KgEventType.ATTRIBUTION_CREATED, tenant_id, {
                    "campaign_id": cid, "arrr_paise": total_paise,
                })

                # VT-563: back-annotate the ORIGINATING run so the implicit-feedback
                # sweep (VT-198/432) can derive owner sentiment from the outcome.
                _back_annotate_run(
                    cur, run_id, tenant_id, total_paise, baseline_paise, now
                )
        if not won:
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
                attribution_row_count=_count_rows(cur, cid),
            )

    # VT-65 PR-2: drain the KG outbox post-commit (immediate, best-effort; the
    # VT-307 sweep is the backstop). Idempotent; never raises.
    from orchestrator.knowledge.kg_emit import drain_kg_events

    drain_kg_events(tenant_id)

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


def _baseline_paise(plan_json: Any) -> int:
    """The campaign's own conservative ARRR prediction (``expected_arrr.low_paise``)
    — the bar the recovered ARRR is judged against by the implicit-feedback sweep
    (outcome > baseline → thumbs_up). ``plan_json`` is the stored CampaignPlan dict
    (mig 018). Missing/malformed → 0 (any recovery then reads as a positive signal).
    """
    if not isinstance(plan_json, dict):
        return 0
    expected = plan_json.get("expected_arrr")
    if not isinstance(expected, dict):
        return 0
    try:
        return max(0, int(expected.get("low_paise")))
    except (TypeError, ValueError):
        return 0


def _back_annotate_run(
    cur,
    run_id: UUID | str | None,
    tenant_id: UUID | str,
    outcome_paise: int,
    baseline_paise: int,
    closed_at: datetime,
) -> None:
    """VT-563: merge the attribution outcome into the ORIGINATING run's
    ``terminal_state_metadata`` so the implicit-feedback sweep can read it.

    Keys match ``implicit_attribution.run_implicit_attribution_sweep`` exactly:
    ``attribution_outcome`` + ``attribution_baseline`` (integer paise; the sweep
    ``float()``-compares them) plus ``attribution_outcome_at`` (ISO ts) — the
    sweep's recency window keys on the latter because the run itself completed
    weeks earlier (at campaign generation), not at close. JSONB ``||`` merge
    preserves any existing metadata. ``pipeline_runs`` is not a watched hot table;
    the ``tenant_id`` predicate guards the BYPASSRLS service conn.
    """
    if run_id is None:
        logger.warning(
            "attribution back-annotation skipped: campaign has no run_id "
            "(tenant=%s)", tenant_id,
        )
        return
    payload = json.dumps({
        "attribution_outcome": int(outcome_paise),
        "attribution_baseline": int(baseline_paise),
        "attribution_outcome_at": closed_at.isoformat(),
    })
    cur.execute(
        "UPDATE pipeline_runs "
        "SET terminal_state_metadata = "
        "    COALESCE(terminal_state_metadata, '{}'::jsonb) || %s::jsonb "
        "WHERE id = %s AND tenant_id = %s",
        (payload, str(run_id), str(tenant_id)),
    )


__all__ = ["close_attribution"]
