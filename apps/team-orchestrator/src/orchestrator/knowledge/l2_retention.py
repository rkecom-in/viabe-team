"""VT-311 — L2 episodic retention (18-month soft-delete sweep).

``run_l2_retention_sweep_body`` stamps ``deleted_at`` on episodic_events older
than the configured window (``TEAM_L2_RETENTION_DAYS``, default 548 ≈ 18 months).
Soft-delete: the ROW stays (audit + hash-chain integrity), but the L2 read path
(``l2_query``) excludes it — DPDP storage-limitation without destroying history.

Cross-tenant MAINTENANCE: runs on the BYPASSRLS service pool in a single UPDATE
(an ops sweep, like l3_construction — not a per-tenant op). Idempotent: a re-run
only marks rows not already soft-deleted. Orthogonal to VT-76 reconstitution.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 548  # ≈ 18 months


def _retention_days() -> int:
    """Config-driven window. Invalid / non-positive → the 18-month default."""
    raw = os.environ.get("TEAM_L2_RETENTION_DAYS", "")
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return days if days > 0 else DEFAULT_RETENTION_DAYS


def run_l2_retention_sweep_body(now: Any = None) -> int:
    """Soft-delete episodic rows older than the retention window; returns the
    number NEWLY soft-deleted. BYPASSRLS service pool (cross-tenant ops sweep).
    ``now`` accepted for parity with the other scheduled bodies (test clock)."""
    from orchestrator.graph import get_pool

    now = now or datetime.now(timezone.utc)
    days = _retention_days()
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE episodic_events SET deleted_at = %s "
            "WHERE deleted_at IS NULL "
            "AND occurred_at < %s - make_interval(days => %s)",
            (now, now, days),
        )
        soft_deleted = cur.rowcount if cur.rowcount is not None else 0
    logger.info(
        "VT-311 L2 retention sweep: soft-deleted %d episodic row(s) older than %dd",
        soft_deleted,
        days,
    )
    return soft_deleted


__all__ = ["DEFAULT_RETENTION_DAYS", "run_l2_retention_sweep_body"]
