"""VT-227 — daily TTL purge of twilio_inbound_replay.

The replay table is append-only by the VT-81 webhook handler; rows
older than 24h are no longer load-bearing for replay defense (5-min
window) but are kept up to 24h for ad-hoc audit. Anything older gets
deleted via this daily scheduled workflow at 3 AM IST (21:30 UTC).

Workflow registration follows the VT-200/VT-215 pattern: workflow
decoration FIRST, scheduled decoration SECOND.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from dbos import DBOS

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

# 3 AM IST = 21:30 UTC (previous day)
_PURGE_CRON = "30 21 * * *"
_PURGE_RETENTION = timedelta(hours=24)


def purge_twilio_inbound_replay_body(
    scheduled_time: datetime, actual_time: datetime
) -> None:
    """Daily 3 AM IST. DELETE twilio_inbound_replay rows older than 24h.

    Logs row-count purged + cutoff timestamp. No PII in log line.
    """
    cutoff = datetime.now(UTC) - _PURGE_RETENTION
    row_count = 0
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM twilio_inbound_replay WHERE received_at < %s",
            (cutoff,),
        )
        row_count = cur.rowcount if hasattr(cur, "rowcount") else 0
    logger.info(
        "twilio_inbound_replay purge: deleted %d rows older than %s",
        row_count,
        cutoff.isoformat(),
    )


def register_twilio_replay_purge_scheduler() -> None:
    """workflow FIRST, scheduled SECOND per VT-215 lesson."""
    DBOS.workflow()(purge_twilio_inbound_replay_body)
    DBOS.scheduled(_PURGE_CRON)(purge_twilio_inbound_replay_body)


__all__ = [
    "purge_twilio_inbound_replay_body",
    "register_twilio_replay_purge_scheduler",
]
