"""VT-198 tier-3: dashboard-driven feedback row writer.

Invoked by the /team/dashboard/feedback UI (web tier) → POSTs run_id +
signal + optional reason; we write to owner_feedback with tier='dashboard'.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_ALLOWED_SIGNALS = {"thumbs_up", "thumbs_down"}


def write_dashboard_feedback(
    *,
    tenant_id: UUID,
    run_id: UUID,
    signal: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist an operator-driven feedback row.

    Args:
        tenant_id: owning tenant UUID (RLS app_current_tenant must match)
        run_id: pipeline_runs.id the operator is rating
        signal: 'thumbs_up' | 'thumbs_down'
        reason: optional 1-line operator-supplied rationale (NO PII;
                caller responsible for ensuring this)
    """
    if signal not in _ALLOWED_SIGNALS:
        raise ValueError(f"signal must be in {_ALLOWED_SIGNALS}, got {signal!r}")

    metadata = {
        "channel": "dashboard",
        "has_reason": reason is not None,
    }
    # We persist the reason in a separate JSONB field so canary A4's
    # PII scrub assertion can find it. Reason text is bounded to 200
    # chars to stop accidental log payloads.
    if reason:
        metadata["reason_excerpt"] = (reason or "")[:200]

    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO owner_feedback
                (tenant_id, run_id, tier, signal, source_metadata)
            VALUES (%s, %s, 'dashboard', %s, %s::jsonb)
            """,
            (
                str(tenant_id),
                str(run_id),
                signal,
                json.dumps(metadata),
            ),
        )
    logger.info(
        "owner_feedback dashboard row written: tenant=%s run=%s signal=%s",
        tenant_id, run_id, signal,
    )
    return {"status": "written", "signal": signal}
