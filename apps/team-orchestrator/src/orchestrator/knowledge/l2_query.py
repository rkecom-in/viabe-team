"""VT-67 — L2 episodic memory retrieval.

Read side of L2: the agent's "what happened recently in this tenant" log. Every
read runs through ``tenant_connection`` (layer-1 RLS + GUC) AND validates the
raw rows via ``assert_tenant_scoped`` (VT-72 layer-2) before mapping — a
cross-tenant row raises ``TenantIsolationError``. Returns frozen ``EpisodicEvent``
models (never raw rows). Hard row cap (``_MAX_ROWS``) so a pathological tenant
can't blow the Composer's token budget.

Consumed by ``context_builder._build_ledger_summary`` (the L2→Composer wire) and
by the VT-76 reconstitution sweep (``events_for_entity``).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from orchestrator._tenant_guard import assert_tenant_scoped
from orchestrator.db import tenant_connection
from orchestrator.knowledge.l2_types import EpisodicEvent

# Hard ceiling on rows returned by any single retrieval — keeps the Composer's
# token budget bounded regardless of how many events a tenant has accumulated.
_MAX_ROWS = 200

_COLS = (
    "id, tenant_id, event_type, summary, payload, "
    "referenced_entity_type, referenced_entity_id, occurred_at"
)


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _to_event(row: dict[str, Any]) -> EpisodicEvent:
    return EpisodicEvent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        event_type=row["event_type"],
        summary=row["summary"],
        payload=row["payload"] or {},
        referenced_entity_type=row["referenced_entity_type"],
        referenced_entity_id=row["referenced_entity_id"],
        occurred_at=row["occurred_at"],
    )


def recent_events(
    tenant_id: UUID | str,
    *,
    limit: int = 50,
    event_types: list[str] | None = None,
) -> list[EpisodicEvent]:
    """Most-recent episodic events for a tenant (newest first).

    ``event_types`` filters in SQL (so a sparse type isn't crowded out by a
    high-volume one). ``limit`` is clamped to ``[1, _MAX_ROWS]``.
    """
    tid = _uuid(tenant_id)
    n = max(1, min(int(limit), _MAX_ROWS))
    # VT-311: exclude retention-expired (soft-deleted) rows from the read path.
    sql = f"SELECT {_COLS} FROM episodic_events WHERE tenant_id = %s AND deleted_at IS NULL"  # noqa: S608 — _COLS is a static literal
    params: list[Any] = [str(tid)]
    if event_types:
        sql += " AND event_type = ANY(%s)"
        params.append(list(event_types))
    sql += " ORDER BY occurred_at DESC, created_at DESC LIMIT %s"
    params.append(n)
    with tenant_connection(tid) as conn:
        raw = conn.execute(sql, tuple(params)).fetchall()
    rows = cast("list[dict[str, Any]]", raw)
    assert_tenant_scoped(rows, tid)
    return [_to_event(r) for r in rows]


def events_for_entity(
    tenant_id: UUID | str,
    referenced_entity_id: UUID | str,
    *,
    limit: int = 50,
) -> list[EpisodicEvent]:
    """Episodic events that reference one entity (newest first).

    The VT-76 reconstitution sweep uses this to find every episodic row that
    points at an opted-out customer before nulling ``referenced_entity_id``.
    """
    tid = _uuid(tenant_id)
    n = max(1, min(int(limit), _MAX_ROWS))
    with tenant_connection(tid) as conn:
        raw = conn.execute(
            f"SELECT {_COLS} FROM episodic_events "  # noqa: S608 — _COLS is a static literal
            "WHERE tenant_id = %s AND referenced_entity_id = %s "
            "AND deleted_at IS NULL "  # VT-311: skip retention-expired rows
            "ORDER BY occurred_at DESC, created_at DESC LIMIT %s",
            (str(tid), str(_uuid(referenced_entity_id)), n),
        ).fetchall()
    rows = cast("list[dict[str, Any]]", raw)
    assert_tenant_scoped(rows, tid)
    return [_to_event(r) for r in rows]


def count_events(tenant_id: UUID | str, *, event_types: list[str] | None = None) -> int:
    """Total episodic events for a tenant (optionally filtered by type)."""
    tid = _uuid(tenant_id)
    # VT-311: count live rows only (retention-expired rows are excluded from reads).
    sql = "SELECT count(*) AS n FROM episodic_events WHERE tenant_id = %s AND deleted_at IS NULL"
    params: list[Any] = [str(tid)]
    if event_types:
        sql += " AND event_type = ANY(%s)"
        params.append(list(event_types))
    with tenant_connection(tid) as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    return int(dict(row)["n"]) if row else 0


__all__ = ["count_events", "events_for_entity", "recent_events"]
