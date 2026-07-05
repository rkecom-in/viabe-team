"""VT-606 — promote the oldest queued task once the active one terminates.

VT-605 (Package 2) built ``create_plan``'s ADMISSION side of the per-tenant objective queue (one
active task per tenant; a later objective while one is active is admitted ``'queued'``) but
explicitly left the DEQUEUE side unbuilt — flagged in that row's own report as landing in VT-606.
This module is that missing half: when a task reaches a terminal status, promote the tenant's
oldest still-``'queued'`` task to ``'planned'`` so the loop picks it up next.

Race-safety mirrors ``plan_store.create_plan``'s own admission check: the ``tenants`` row
``FOR UPDATE`` lock serializes concurrent callers for the SAME tenant, so "is there still an
active task" and "promote the oldest queued one" happen atomically — no double-promotion, no
promoting into an already-active slot.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.manager import task_store
from orchestrator.observability.tm_audit import emit_tm_audit

logger = logging.getLogger("orchestrator.manager.queue_promotion")


def _uuid(row: Any) -> UUID:
    val = row["id"] if isinstance(row, dict) else row[0]
    return val if isinstance(val, UUID) else UUID(str(val))


def promote_next_queued_task(tenant_id: UUID | str) -> UUID | None:
    """Promote the tenant's oldest ``'queued'`` task to ``'planned'`` — but ONLY if no task is
    currently active (mirrors ``create_plan``'s own admission gate). Returns the promoted task's
    id, or ``None`` when there is nothing to promote (queue empty) or the tenant is still busy
    (an active task exists — the caller should retry after THAT one also terminates).
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute("SELECT id FROM tenants WHERE id = %s FOR UPDATE", (str(tenant_id),)).fetchone()

        active = conn.execute(
            "SELECT 1 FROM manager_tasks WHERE tenant_id = %s AND status = ANY(%s) LIMIT 1",
            (str(tenant_id), list(task_store.TASK_ACTIVE)),
        ).fetchone()
        if active is not None:
            return None  # still busy — nothing promotes until that task also terminates

        oldest_queued = conn.execute(
            "SELECT id FROM manager_tasks WHERE tenant_id = %s AND status = 'queued' "
            "ORDER BY created_at ASC LIMIT 1",
            (str(tenant_id),),
        ).fetchone()
        if oldest_queued is None:
            return None  # queue empty
        task_id = _uuid(oldest_queued)

        conn.execute(
            "UPDATE manager_tasks SET status = 'planned', version = version + 1, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = 'queued'",
            (str(tenant_id), str(task_id)),
        )

        emit_tm_audit(
            event_layer="does",
            event_kind="queued_task_promoted",
            actor="team_manager",
            tenant_id=tenant_id,
            summary=f"promoted queued task={task_id} to planned",
            decision={"task_id": str(task_id)},
            conn=conn,
        )
    logger.info("queue_promotion: promoted task=%s to planned (tenant=%s)", task_id, tenant_id)
    return task_id


__all__ = ["promote_next_queued_task"]
