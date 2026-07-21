"""VT-683 P2 — the session-first owner-comms queue (data layer).

Out-of-window owner comms (approval asks, notices, ready reports) no longer push a Meta template;
they QUEUE here and drain at idle pace inside an open 24h session (the drainer is P2b). This module
is the durable CRUD over ``owner_comms_queue`` (migration 178).

POINT A (Fazal 2026-07-21): an APPROVAL's decision timeout clock starts at DELIVERY, never at
enqueue — the owner can't time out on an ask he never saw. So ``decision_deadline_at`` is set in
``mark_delivered`` (= delivered_at + TTL), NOT in ``enqueue``. The action's own business freshness
is re-checked separately at resolution (P2c) — the queue never makes a stale action fresh.

DB access: per-tenant ops (enqueue / next / mark_delivered) run under the caller's tenant-scoped
connection (RLS ``tenant_id = app_current_tenant()``); the cross-tenant hygiene sweeps
(``drop_stale`` / ``overdue_delivered_approvals``) run service-role. Every write is fail-soft at the
call sites that need it (the drainer), but the CRUD here raises so a caller in a txn sees failures.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

# approval > report > notice by default (the drainer prefers decisions over information).
DEFAULT_PRIORITY = {"approval": 100, "report": 50, "notice": 10}

# POINT A default: once an approval is DELIVERED, the owner gets this long to decide before the
# underlying ask is expired (P2c wires the actual approval expiry). Tunable per-enqueue later.
DECISION_TTL = timedelta(hours=48)

# Honest-expiry: a queued item never DELIVERED within this bound is dropped (the owner never came
# back to open a session). Generous — the wake-up loop (P3) actively pulls the owner back daily.
MAX_QUEUE_AGE = timedelta(days=7)


def _default_priority(kind: str) -> int:
    return DEFAULT_PRIORITY.get(kind, 0)


def enqueue(
    tenant_id: UUID | str,
    *,
    kind: str,
    payload: dict[str, Any],
    priority: int | None = None,
    decision_ref: dict[str, Any] | None = None,
    conn: Any = None,
) -> UUID:
    """Queue one owner-comms item. Returns its id.

    ``conn`` — when the caller already holds a tenant-scoped txn (e.g. ``arm_pause_request`` arming
    an approval), pass it so the enqueue commits atomically with the arm; otherwise a fresh
    tenant_connection is opened. ``decision_ref`` links an 'approval' to its real ask object
    ({"kind": "pending_approval", "id": "…"}) for resolution + the freshness gate.
    """
    from psycopg.types.json import Jsonb

    item_id = uuid4()
    prio = priority if priority is not None else _default_priority(kind)
    params = (
        str(item_id), str(tenant_id), kind, Jsonb(payload), prio,
        Jsonb(decision_ref) if decision_ref is not None else None,
    )
    sql = (
        "INSERT INTO owner_comms_queue (id, tenant_id, kind, payload, priority, decision_ref) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as own:
            own.execute(sql, params)
    return item_id


def next_deliverable(tenant_id: UUID | str, *, conn: Any = None) -> dict[str, Any] | None:
    """The highest-priority still-queued item for this tenant (priority DESC, oldest first), or None.

    Read-only; the drainer calls this then ``mark_delivered`` after the send succeeds.
    """
    sql = (
        "SELECT id, kind, payload, decision_ref, priority, queued_at "
        "FROM owner_comms_queue "
        "WHERE tenant_id = %s AND status = 'queued' "
        "ORDER BY priority DESC, queued_at ASC "
        "LIMIT 1"
    )

    def _run(c: Any) -> dict[str, Any] | None:
        row = c.execute(sql, (str(tenant_id),)).fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        return {
            "id": row[0], "kind": row[1], "payload": row[2],
            "decision_ref": row[3], "priority": row[4], "queued_at": row[5],
        }

    if conn is not None:
        return _run(conn)
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as own:
        return _run(own)


def mark_delivered(
    tenant_id: UUID | str,
    item_id: UUID | str,
    *,
    kind: str,
    message_sid: str | None,
    decision_ttl: timedelta = DECISION_TTL,
    conn: Any = None,
) -> None:
    """Mark an item delivered — POINT A: the approval decision clock starts NOW.

    Sets ``delivered_at = now()`` for all kinds; for ``kind='approval'`` also sets
    ``decision_deadline_at = now() + decision_ttl`` (the delivered-approval deadline the P2c expiry
    sweep reads). A non-approval item gets no deadline. Idempotent-safe: only flips a still-'queued'
    row (a redelivery can't reset an already-started decision clock).
    """
    ttl_seconds = int(decision_ttl.total_seconds()) if kind == "approval" else None
    sql = (
        "UPDATE owner_comms_queue "
        "SET status = 'delivered', delivered_at = now(), message_sid = %s, "
        "    decision_deadline_at = CASE WHEN %s IS NULL THEN NULL "
        "                                ELSE now() + make_interval(secs => %s) END "
        "WHERE tenant_id = %s AND id = %s AND status = 'queued'"
    )
    params = (message_sid, ttl_seconds, ttl_seconds, str(tenant_id), str(item_id))
    if conn is not None:
        conn.execute(sql, params)
        return
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as own:
        own.execute(sql, params)


def drop_stale(*, max_age: timedelta = MAX_QUEUE_AGE, pool: Any = None) -> int:
    """Honest-expiry: drop every still-'queued' item never delivered within ``max_age`` (the owner
    never opened a session). Service-role cross-tenant. Returns the number dropped. NEVER a silent
    vanish — the row stays with status='dropped' + dropped_reason='max_age' for audit.
    """
    from orchestrator.graph import get_pool

    p = pool if pool is not None else get_pool()
    age_seconds = int(max_age.total_seconds())
    with p.connection() as conn:
        cur = conn.execute(
            "UPDATE owner_comms_queue "
            "SET status = 'dropped', dropped_reason = 'max_age' "
            "WHERE status = 'queued' AND queued_at < now() - make_interval(secs => %s)",
            (age_seconds,),
        )
        return cur.rowcount if cur.rowcount is not None else 0


def overdue_delivered_approvals(*, pool: Any = None) -> list[dict[str, Any]]:
    """Service-role reader: every DELIVERED approval whose ``decision_deadline_at`` has passed.

    P2c consumes this to expire the UNDERLYING approval object (via ``decision_ref``) — the honest-
    expiry of an ask the owner saw but never answered. This module only IDENTIFIES them (the queue
    is the delivery ledger; the pending-approvals row stays the money authority).
    """
    from orchestrator.graph import get_pool

    p = pool if pool is not None else get_pool()
    with p.connection() as conn:
        rows = conn.execute(
            "SELECT id, tenant_id, decision_ref FROM owner_comms_queue "
            "WHERE status = 'delivered' AND kind = 'approval' "
            "  AND decision_deadline_at IS NOT NULL AND decision_deadline_at < now()"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append({"id": r["id"], "tenant_id": r["tenant_id"], "decision_ref": r["decision_ref"]})
        else:
            out.append({"id": r[0], "tenant_id": r[1], "decision_ref": r[2]})
    return out


__all__ = [
    "DECISION_TTL",
    "DEFAULT_PRIORITY",
    "MAX_QUEUE_AGE",
    "drop_stale",
    "enqueue",
    "mark_delivered",
    "next_deliverable",
    "overdue_delivered_approvals",
]
