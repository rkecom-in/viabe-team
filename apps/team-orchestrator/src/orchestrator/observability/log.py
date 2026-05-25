"""pipeline_log writer (VT-102).

Single entry point: ``log_event(event_type, run_id, tenant_id, severity,
component, payload, duration_ms=None)``. Fire-and-forget — the call always
returns immediately and the actual INSERT runs out-of-band so observability
never blocks the orchestrator's hot path.

Failure isolation: on insert failure, write a structured stderr breadcrumb
and drop the row. The pipeline does NOT see the failure. (Phase-1 spec —
``pipeline_log_failures`` sentinel table is Phase 2 per brief.)

PII redaction: ``payload`` flows through ``redact_for_log`` (alias of
``redact_for_langsmith``) before serialisation. Bypass requires replacing
this writer; code review catches that.

Schema validation: soft. Invalid payloads still write, but with an injected
``payload_validation_failed: true`` flag carrying the validator's error list.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.graph import get_pool
from orchestrator.observability.event_schemas import validate as validate_schema
from orchestrator.observability.pii import redact_for_log


# Allowed values match the migration's CHECK constraint.
_VALID_SEVERITIES = frozenset({"debug", "info", "warn", "error", "critical"})


def log_event(
    event_type: str,
    run_id: UUID | str,
    tenant_id: UUID | str | None,
    severity: str,
    component: str,
    payload: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Schedule an append-only write to ``pipeline_log``. Never raises.

    The actual INSERT runs in the background:

    - If an asyncio event loop is already running in this thread, schedule
      via ``loop.create_task``.
    - Otherwise, dispatch on a daemon thread so the caller still returns
      immediately even when invoked from synchronous DBOS step bodies.

    Both paths funnel through :func:`_do_insert`. Failures stderr-warn + drop.
    """
    if severity not in _VALID_SEVERITIES:
        _stderr_warn(f"log_event: invalid severity {severity!r}, coercing to 'info'")
        severity = "info"

    safe_payload = _prepare_payload(event_type, payload or {})

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Inside a running loop — schedule as a task. The coroutine itself
        # offloads the blocking psycopg call via run_in_executor so we
        # don't stall the loop.
        loop.create_task(_do_insert_async(event_type, run_id, tenant_id, severity, component, safe_payload, duration_ms))
    else:
        # No live loop — fire on a daemon thread. Keeps log_event sync-safe.
        thread = threading.Thread(
            target=_do_insert_sync,
            args=(event_type, run_id, tenant_id, severity, component, safe_payload, duration_ms),
            daemon=True,
        )
        thread.start()


def _prepare_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Redact + soft-validate. Returns the dict that will be persisted."""
    redacted = redact_for_log(payload)
    if not isinstance(redacted, dict):
        # redact_for_log preserves structure, but defend against future changes.
        redacted = {"payload_redaction_unexpected_type": str(type(redacted).__name__)}

    ok, errors = validate_schema(event_type, redacted)
    if not ok:
        redacted = dict(redacted)
        redacted["payload_validation_failed"] = True
        redacted["payload_validation_errors"] = errors
    return redacted


async def _do_insert_async(
    event_type: str,
    run_id: UUID | str,
    tenant_id: UUID | str | None,
    severity: str,
    component: str,
    payload: dict[str, Any],
    duration_ms: int | None,
) -> None:
    """Async wrapper that runs the blocking insert on the default executor."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _do_insert_sync,
        event_type,
        run_id,
        tenant_id,
        severity,
        component,
        payload,
        duration_ms,
    )


def _do_insert_sync(
    event_type: str,
    run_id: UUID | str,
    tenant_id: UUID | str | None,
    severity: str,
    component: str,
    payload: dict[str, Any],
    duration_ms: int | None,
) -> None:
    """Synchronous insert path. Catches every exception and drops the row."""
    try:
        if tenant_id is None:
            # Workspace-level event — service-role connection, bypasses RLS.
            with get_pool().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_log
                        (run_id, tenant_id, event_type, severity,
                         component, payload, duration_ms)
                    VALUES (%s, NULL, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(run_id),
                        event_type,
                        severity,
                        component,
                        Jsonb(payload),
                        duration_ms,
                    ),
                )
        else:
            # Tenant-scoped — app_role connection, RLS enforced.
            with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_log
                        (run_id, tenant_id, event_type, severity,
                         component, payload, duration_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(run_id),
                        str(tenant_id),
                        event_type,
                        severity,
                        component,
                        Jsonb(payload),
                        duration_ms,
                    ),
                )
    except BaseException as exc:  # noqa: BLE001 - failure isolation
        _stderr_warn(
            f"pipeline_log insert failed: event_type={event_type!r} "
            f"run_id={run_id} tenant_id={tenant_id} "
            f"exc={type(exc).__name__}: {exc}"
        )


def _stderr_warn(msg: str) -> None:
    """One-line stderr breadcrumb. Never raises."""
    try:
        print(f"[observability/log] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Retention sweep — service-role only.
# ---------------------------------------------------------------------------

def purge_pipeline_log_older_than(days: int = 90) -> int:
    """Delete ``pipeline_log`` rows older than ``days`` and return the count.

    Invokes the migration's ``purge_pipeline_log_older_than`` SECURITY DEFINER
    function, which runs under the function's owner (a privileged role) so
    bypass-RLS is structural. The Python wrapper also opens a service-role
    connection — if called under app_role, the SQL function still works
    (SECURITY DEFINER), but the cursor's GRANTs prevent the call.
    """
    if days < 1:
        raise ValueError(f"retention days must be >= 1, got {days}")
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT purge_pipeline_log_older_than(%s) AS deleted", (days,))
        row = cur.fetchone()
        if row is None:
            return 0
        # Pool default row_factory may be dict_row; access by name for safety.
        if isinstance(row, dict):
            deleted = row["deleted"]
        else:
            deleted = row[0]
        return int(deleted) if deleted is not None else 0


__all__ = ["log_event", "purge_pipeline_log_older_than"]
