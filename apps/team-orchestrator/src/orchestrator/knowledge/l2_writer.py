"""VT-66 — L2 episodic memory writer.

``record_episodic_event`` appends one row to episodic_events (templated summary,
NOT LLM). Idempotent on ``event_id`` (the kg_events outbox event that produced
it) so the dual-projection re-applies exactly-once on a re-drain (Cowork req 1).
Tenant-scoped via tenant_connection + assert_tenant_scoped (VT-72 layer-2).
CL-390: payloads carry NO raw PII (ids/counts/amounts/hashes only).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator._tenant_guard import assert_tenant_scoped
from orchestrator.db import tenant_connection
from orchestrator.knowledge.l2_types import render_summary


def record_episodic_event(
    tenant_id: UUID | str,
    event_type: str,
    *,
    payload: dict[str, Any],
    referenced_entity_type: str | None = None,
    referenced_entity_id: UUID | str | None = None,
    summary: str | None = None,
    occurred_at: datetime | None = None,
    event_id: UUID | str | None = None,
    conn: Any = None,
) -> UUID:
    """Append an episodic event. Idempotent on (tenant_id, event_id) when
    event_id is set. Returns the episodic row id.

    ``conn`` (optional) lets a caller append within an existing txn (atomic with
    a source write); else a fresh tenant_connection is used.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    summary = summary if summary is not None else render_summary(event_type, payload)
    occurred_at = occurred_at or datetime.now(UTC)

    def _do(c: Any) -> UUID:
        row = c.execute(
            """
            INSERT INTO episodic_events
              (tenant_id, event_id, event_type, summary, payload,
               referenced_entity_type, referenced_entity_id, occurred_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, event_id) WHERE event_id IS NOT NULL
            DO NOTHING
            RETURNING id, tenant_id
            """,
            (
                str(tid),
                str(event_id) if event_id is not None else None,
                event_type, summary, Jsonb(payload),
                referenced_entity_type,
                str(referenced_entity_id) if referenced_entity_id is not None else None,
                occurred_at,
            ),
        ).fetchone()
        if row is None:
            # ON CONFLICT no-op (already recorded for this event_id) — fetch it.
            existing = c.execute(
                "SELECT id, tenant_id FROM episodic_events "
                "WHERE tenant_id = %s AND event_id = %s",
                (str(tid), str(event_id)),
            ).fetchone()
            d = dict(existing)
            assert_tenant_scoped([d], tid)
            return d["id"] if isinstance(d["id"], UUID) else UUID(str(d["id"]))
        d = dict(row)
        assert_tenant_scoped([d], tid)
        return d["id"] if isinstance(d["id"], UUID) else UUID(str(d["id"]))

    if conn is not None:
        return _do(conn)
    with tenant_connection(tid) as own:
        return _do(own)


__all__ = ["record_episodic_event"]
