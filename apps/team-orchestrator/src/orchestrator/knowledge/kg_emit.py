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
from orchestrator.knowledge.kg_vocab import KgEventType
from orchestrator.knowledge.l2_types import L2EventType
from orchestrator.knowledge.l2_writer import record_episodic_event

logger = logging.getLogger(__name__)

# VT-66/67 — dual-projection map: which kg_events outbox types ALSO project to L2
# episodic_events, and how. Only the OVERLAPPING types are here (campaign_sent,
# attribution_created); the ~10 agent-decision L2 types get their own emit sites
# in VT-309 (the cross-cutting live-path pass, plan-reviewed separately). Each
# entry maps kg_event_type -> (l2_event_type, referenced_entity_type, payload_fn).
# payload_fn returns the PII-free L2 payload (ids/counts/amounts only — CL-390).
_L2_PROJECTION: dict[str, tuple[str, str, Any]] = {
    KgEventType.CAMPAIGN_SENT: (
        L2EventType.CAMPAIGN_SENT,
        "campaign",
        lambda p: {
            "campaign_id": p.get("campaign_id"),
            "recipient_count": len(p.get("customer_ids") or []),
        },
    ),
    KgEventType.ATTRIBUTION_CREATED: (
        L2EventType.ATTRIBUTION_CLOSED,
        "campaign",
        lambda p: {
            "campaign_id": p.get("campaign_id"),
            "arrr_paise": p.get("arrr_paise"),
        },
    ),
}


def _project_l2(tenant_id: UUID, event_id: UUID, kg_event_type: str, payload: dict[str, Any]) -> bool:
    """Project an overlapping outbox event to L2 episodic_events (idempotent on
    (tenant_id, event_id)). Returns True if there is nothing to project OR the
    episodic row was written/already-present; False on a real write failure (so
    the drain leaves the event undrained → re-drain retries → exactly-once).

    Non-overlapping kg event types (tenant/customer/transaction/campaign_created)
    have no L2 projection yet → True (nothing to do; the L1 projection covers them).
    """
    spec = _L2_PROJECTION.get(kg_event_type)
    if spec is None:
        return True
    l2_type, ref_type, payload_fn = spec
    try:
        l2_payload = payload_fn(payload)
        record_episodic_event(
            tenant_id,
            l2_type,
            payload=l2_payload,
            referenced_entity_type=ref_type,
            referenced_entity_id=l2_payload.get("campaign_id"),
            event_id=event_id,
        )
        return True
    except Exception:  # noqa: BLE001 — drain is best-effort; leave undrained for re-drain
        logger.exception(
            "VT-66 L2 projection failed (tenant=%s event=%s type=%s)",
            tenant_id, event_id, kg_event_type,
        )
        return False


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
    from orchestrator.observability.tm_audit import emit_tm_audit
    eid = uuid4()
    conn.execute(
        """
        INSERT INTO kg_events (event_id, event_type, tenant_id, payload)
        VALUES (%s, %s, %s, %s)
        """,
        (str(eid), event_type, str(tenant_id), Jsonb(payload)),
    )
    emit_tm_audit(
        event_layer="does",
        event_kind="memory_write",
        actor="team_manager",
        tenant_id=tenant_id,
        action={"event_id": str(eid), "event_type": event_type},
        summary=f"KG event written: {event_type}",
        conn=None,
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
            eid = UUID(str(rd["event_id"]))
            payload = rd.get("payload") or {}
            # Projection 1 — L1 entities/edges (idempotent via external_key + ledger).
            result = process_kg_event(KgEvent(eid, rd["event_type"], tid, payload))
            # Projection 2 — L2 episodic (overlapping event types only; idempotent
            # via episodic_events UNIQUE(tenant_id, event_id)). VT-309 adds the
            # agent-decision event types + their emit sites.
            l2_ok = _project_l2(tid, eid, rd["event_type"], payload)
            # Mark drained ONLY after BOTH projections succeed — a crash between
            # them leaves it undrained → re-drain re-runs both (each idempotent)
            # → exactly-once in L1 AND L2 (Cowork req-1).
            if result in ("processed", "skipped") and l2_ok:
                with tenant_connection(tid) as conn:
                    conn.execute(
                        "UPDATE kg_events SET drained_at = now() WHERE event_id = %s",
                        (str(eid),),
                    )
                drained += 1
            else:
                failed += 1
    except Exception:  # noqa: BLE001 — drain is best-effort; the sweep (VT-307) retries
        logger.exception("VT-65 drain_kg_events failed (tenant=%s)", tid)
    return {"drained": drained, "failed": failed}


__all__ = ["drain_kg_events", "emit_kg_event"]
