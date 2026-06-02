"""VT-298 — resolve the assigned VTR Telegram recipients for a tenant's alert.

The autonomous watchdog must reach the ASSIGNED VTR immediately (Fazal). Recipient
resolution chains the two deny-all operator substrates:

    tenant → operator_assignments (active, mig 072) → operator_telegram (verified, mig 075)

Only VERIFIED chat_ids are returned (we never message an unverified chat — Cowork DECISION 1).
Both tables are deny-all FORCE RLS → read via the service-role pool (RLS-bypassing). The OPS
chat keeps its existing routing; this is the ADDITIONAL assigned-VTR fan-out (DECISION 2:
BOTH). CL-390: returns chat_ids only; no PII.
"""

from __future__ import annotations

import logging
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


def resolve_assigned_vtr_chat_ids(tenant_id: UUID | str) -> list[str]:
    """Verified Telegram chat_ids of the VTRs actively assigned to this tenant.

    Empty list when the tenant has no assigned VTR with a verified binding — the caller
    still sends to the OPS chat, so an empty result is not an error (fail-open for the OPS
    channel, fail-CLOSED for the VTR channel: no verified binding → no VTR send).
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ot.chat_id
            FROM operator_assignments oa
            JOIN operator_telegram ot ON ot.operator_id = oa.operator_id
            WHERE oa.tenant_id = %s
              AND oa.unassigned_at IS NULL
              AND ot.verified_at IS NOT NULL
            """,
            (str(tenant_id),),
        )
        rows = cur.fetchall()
    chat_ids: list[str] = []
    for raw in rows:
        rd = dict(raw) if not isinstance(raw, dict) else raw
        chat = rd.get("chat_id") if isinstance(rd, dict) else raw[0]
        if chat:
            chat_ids.append(str(chat))
    return chat_ids
