"""VT-300 — run-control EFFECTING leg (graph-side consumption of run_controls).

Honest coarse semantics (Fazal + adversarial review, 2026-06-03): the agent brain runs inside ONE
synchronous DBOS step, so there is NO mid-step suspend. Control converges only at top-level graph
NODE boundaries. The highest-value boundary is BEFORE the campaign send fan-out: a VTR who issues
pause/steer/override can stop a run BEFORE it sends to customers. Finer (mid-ReAct) control needs a
graph restructure (XL) — out of scope; flagged on the VT-300 row.

`consume_pending_control` atomically claims the oldest un-consumed control for a run (FOR UPDATE SKIP
LOCKED → no double-consume, no double-apply). The caller decides the effect; v1: ANY pending control
HOLDS the send (the VTR intervened → do not auto-send). run_controls is deny-all RLS → service pool.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def consume_pending_control(run_id: UUID | str, *, pool: Any = None) -> dict[str, Any] | None:
    """Atomically claim + mark consumed the oldest 'requested' control for this run.

    Returns {control_type, directive} or None. Idempotent under concurrency (SKIP LOCKED +
    single-row claim): a control is applied at most once.
    """
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE run_controls
               SET status = 'consumed', consumed_at = now()
             WHERE id = (
                 SELECT id FROM run_controls
                  WHERE run_id = %s AND status = 'requested'
                  ORDER BY requested_at
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING control_type, directive
            """,
            (str(run_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return {"control_type": row["control_type"], "directive": row.get("directive")}
    return {"control_type": row[0], "directive": row[1]}


def should_hold_send(control: dict[str, Any] | None) -> bool:
    """v1 effect: ANY pending control (pause/steer/override) HOLDS the send fan-out — the VTR
    intervened, so the run must NOT auto-send to customers; it's surfaced for review instead."""
    return control is not None
