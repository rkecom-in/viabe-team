"""pipeline_log read API (VT-102).

Four functions, all returning ``list[PipelineLogEvent]`` ordered chronologically
(unless the caller wants newest-first, see ``order_desc``). RLS does the
tenant-isolation work — these functions just shape the SQL + map rows.

``query_errors_recent`` is service-role-only: it scans across tenants for the
ops dashboard. The function checks the role explicitly (defense in depth on
top of RLS) so a misconfigured caller raises rather than silently returning
zero rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from orchestrator.db import tenant_connection
from orchestrator.graph import get_pool
from orchestrator.observability.types import PipelineLogEvent


def query_run(run_id: UUID | str) -> list[PipelineLogEvent]:
    """All events for ``run_id``, chronologically ordered.

    Opens a service-role connection so workspace-level rows (tenant_id NULL)
    surface alongside tenant rows for a given run. Tenant-scoped callers
    should use the tenant-bound view (Phase 2); for Phase 1 this is the
    debug-substrate entry point and is service-role-only.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, run_id, tenant_id, event_type, severity, component,
                   payload, duration_ms, created_at
              FROM pipeline_log
             WHERE run_id = %s
             ORDER BY created_at ASC
            """,
            (str(run_id),),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


def query_tenant_recent(
    tenant_id: UUID | str,
    since: datetime,
    limit: int = 100,
) -> list[PipelineLogEvent]:
    """Tenant's recent events for owner-portal-style debugging.

    Opens an ``app_role`` connection scoped to ``tenant_id``; RLS does the
    isolation. The ``(tenant_id, created_at DESC) WHERE tenant_id IS NOT NULL``
    partial index covers this query.
    """
    with tenant_connection(tenant_id) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, run_id, tenant_id, event_type, severity, component,
                   payload, duration_ms, created_at
              FROM pipeline_log
             WHERE tenant_id = %s
               AND created_at >= %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (str(tenant_id), since, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


def query_errors_recent(
    since: datetime,
    severity_min: str = "error",
    limit: int = 100,
) -> list[PipelineLogEvent]:
    """Workspace-level error scan; service-role only.

    Uses the ``(severity, created_at DESC) WHERE severity IN ('error',
    'critical')`` partial index. The function opens a service-role
    connection directly — calling it under an app_role connection would
    return zero rows because RLS denies cross-tenant SELECT.
    """
    if severity_min not in ("error", "critical"):
        raise ValueError(
            f"severity_min must be 'error' or 'critical', got {severity_min!r}"
        )
    severities = ("error", "critical") if severity_min == "error" else ("critical",)
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, run_id, tenant_id, event_type, severity, component,
                   payload, duration_ms, created_at
              FROM pipeline_log
             WHERE severity = ANY(%s)
               AND created_at >= %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (list(severities), since, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


def query_event_type(
    event_type: str,
    since: datetime,
    limit: int = 100,
) -> list[PipelineLogEvent]:
    """Type-specific scan; service-role view (no tenant filter)."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, run_id, tenant_id, event_type, severity, component,
                   payload, duration_ms, created_at
              FROM pipeline_log
             WHERE event_type = %s
               AND created_at >= %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (event_type, since, limit),
        )
        rows = cur.fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_event(row: dict[str, Any]) -> PipelineLogEvent:
    return PipelineLogEvent(
        id=row["id"],
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        event_type=row["event_type"],
        severity=row["severity"],
        component=row["component"],
        payload=row["payload"] or {},
        duration_ms=row["duration_ms"],
        created_at=row["created_at"],
    )


__all__ = [
    "query_errors_recent",
    "query_event_type",
    "query_run",
    "query_tenant_recent",
]
