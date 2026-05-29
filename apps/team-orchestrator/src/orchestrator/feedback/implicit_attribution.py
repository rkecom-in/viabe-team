"""VT-198 tier-1: implicit feedback derived from attribution outcome.

Scheduled daily. For each tenant with owner-approved campaigns in the
last 7 days, derive thumbs_up / thumbs_down from the campaign's
attribution_outcome vs baseline. Writes one row per (tenant, run, tier)
— partial unique index on migration 041 enforces idempotency at the DB.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


def run_implicit_attribution_sweep() -> dict[str, int]:
    """Sweep the last 7d of completed campaigns + write implicit rows.

    Returns counts: {'considered': N, 'written': M, 'skipped_no_outcome': K}
    Idempotent — re-running the sweep does not double-write (partial
    unique index on (tenant_id, run_id, tier='implicit')).
    """
    pool = get_pool()
    considered = 0
    written = 0
    skipped = 0

    with pool.connection() as conn, conn.cursor() as cur:
        # Find completed campaigns in last 7d with attribution data.
        # Minimal schema assumption: pipeline_runs has tenant_id, status,
        # completed_at; attribution_outcome lives in terminal_state_metadata
        # JSONB. If attribution substrate not yet wired, this returns 0
        # rows + sweep is a no-op (acceptable Phase 1).
        cur.execute(
            """
            SELECT id AS run_id,
                   tenant_id::text AS tenant_id,
                   terminal_state_metadata
            FROM pipeline_runs
            WHERE status = 'completed'
              AND completed_at >= now() - interval '7 days'
              AND terminal_state_metadata IS NOT NULL
              AND terminal_state_metadata ? 'attribution_outcome'
            """
        )
        rows = cur.fetchall()

    for row in rows:
        considered += 1
        row_dict = row if isinstance(row, dict) else {
            "run_id": row[0], "tenant_id": row[1], "terminal_state_metadata": row[2],
        }
        meta = row_dict.get("terminal_state_metadata") or {}
        outcome: Any = meta.get("attribution_outcome") if isinstance(meta, dict) else None
        baseline = meta.get("attribution_baseline") if isinstance(meta, dict) else None
        if outcome is None or baseline is None:
            skipped += 1
            continue

        try:
            signal = "thumbs_up" if float(outcome) > float(baseline) else "thumbs_down"
        except (TypeError, ValueError):
            skipped += 1
            continue

        try:
            with pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO owner_feedback
                        (tenant_id, run_id, tier, signal, source_metadata)
                    VALUES (%s, %s, 'implicit', %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        row_dict["tenant_id"],
                        row_dict["run_id"],
                        signal,
                        # NO PII — shape only (outcome + baseline are
                        # internal metrics, OK to log)
                        '{"derived_from":"attribution_outcome"}',
                    ),
                )
                written += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "implicit_attribution write failed (tenant=%s, run=%s)",
                row_dict["tenant_id"],
                row_dict["run_id"],
            )

    logger.info(
        "implicit_attribution sweep: considered=%d written=%d skipped=%d",
        considered, written, skipped,
    )
    return {"considered": considered, "written": written, "skipped_no_outcome": skipped}
