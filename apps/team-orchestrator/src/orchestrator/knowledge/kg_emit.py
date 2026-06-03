"""VT-65 PR-2 — transactional outbox emit + drain for KG events.

``emit_kg_event(conn, ...)`` writes an outbox row using the CALLER's connection,
so it is ATOMIC with the source write (same txn → commit/rollback together).
``drain_kg_events(tenant_id)`` applies undrained rows via the idempotent
``process_kg_event`` consumer (kg_events_processed) and stamps ``drained_at`` —
run post-commit (immediate, best-effort) + by the scheduled sweep (VT-307).

Atomicity contract (Cowork 20260603T174500Z): callers MUST emit inside an open
``conn.transaction()`` on the same connection as the source write. Sites that
autocommit per-statement were wrapped in PR-2 (see the per-site classification
in the PR report). ``drain`` runs on a SEPARATE connection AFTER the source txn
commits — a still-uncommitted/rolled-back event is simply not visible to drain.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.knowledge.kg_population import KgEvent, process_kg_event

logger = logging.getLogger(__name__)


def emit_kg_event(
    conn: Any,
    event_type: str,
    tenant_id: UUID | str,
    payload: dict[str, Any],
) -> UUID:
    """Write a KG event to the outbox on the CALLER's connection (same txn).

    MUST be called inside the source write's transaction so the event is atomic
    with it. Returns the event_id.
    """
    eid = uuid4()
    conn.execute(
        """
        INSERT INTO kg_events (event_id, event_type, tenant_id, payload)
        VALUES (%s, %s, %s, %s)
        """,
        (str(eid), event_type, str(tenant_id), Jsonb(payload)),
    )
    return eid


def drain_kg_events(tenant_id: UUID | str, *, limit: int = 500) -> dict[str, int]:
    """Apply undrained outbox events for a tenant via the idempotent consumer.

    Runs on its own connection (post source-commit). Idempotent: process_kg_event
    skips already-processed event_ids, so a re-drain never double-applies. Returns
    {'drained': n, 'failed': m}. Never raises — drain is best-effort.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    drained = failed = 0
    try:
        with tenant_connection(tid) as conn:
            rows = conn.execute(
                "SELECT event_id, event_type, payload FROM kg_events "
                "WHERE tenant_id = %s AND drained_at IS NULL "
                "ORDER BY emitted_at LIMIT %s",
                (str(tid), limit),
            ).fetchall()
        for r in rows:
            rd = dict(r)
            result = process_kg_event(
                KgEvent(
                    UUID(str(rd["event_id"])), rd["event_type"], tid, rd.get("payload") or {}
                )
            )
            if result in ("processed", "skipped"):
                with tenant_connection(tid) as conn:
                    conn.execute(
                        "UPDATE kg_events SET drained_at = now() WHERE event_id = %s",
                        (str(rd["event_id"]),),
                    )
                drained += 1
            else:
                failed += 1
    except Exception:  # noqa: BLE001 — drain is best-effort; the sweep (VT-307) retries
        logger.exception("VT-65 drain_kg_events failed (tenant=%s)", tid)
    return {"drained": drained, "failed": failed}


__all__ = ["drain_kg_events", "emit_kg_event"]
