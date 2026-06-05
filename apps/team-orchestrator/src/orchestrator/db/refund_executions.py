"""VT-93 — refund_executions ledger wrapper (db/ layer; gate-exempt).

All SQL for the tenant-scoped ``refund_executions`` table lives here. The
``check_no_direct_tenant_db_access`` gate watches the table; the ``db/`` subtree
is the sanctioned access point, so ``billing/refund_executor.py`` (business
logic) calls these functions and contains NO refund_executions SQL.

Idempotency primitive: :func:`claim_or_get` serializes per-tenant refund attempts
via ``pg_advisory_xact_lock`` + ``INSERT ... ON CONFLICT DO NOTHING`` +
``SELECT ... FOR UPDATE``, so two concurrent ``execute_refund`` calls cannot both
issue a refund (the PK ``(tenant_id, refund_reason)`` is the dedup key).

Mutations only ever touch a non-``completed`` row (the executor stops before
re-touching a completed one); the migration-099 immutability trigger blocks any
mutation of a ``completed`` row for every role except the DSR purge session.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from orchestrator.db import tenant_connection


def claim_or_get(
    conn: Any,
    tenant_id: UUID | str,
    refund_reason: str,
    total_refund_paise: int,
    day39_evaluation_id: UUID | str | None,
) -> tuple[dict[str, Any], bool]:
    """Atomically claim (or fetch) the refund-execution row for this tenant+reason.

    Must run inside a tenant_connection transaction. Takes a per-tenant
    advisory lock, INSERTs a ``pending`` row if none exists (ON CONFLICT DO
    NOTHING), then SELECTs FOR UPDATE so the caller holds the row for the rest of
    the transaction. Returns ``(row, created)`` — ``created`` is True only when
    this call inserted the row (first-claimer); False when a prior execution row
    already existed (idempotent re-entry).
    """
    tid = str(tenant_id)
    # Serialize all refund work for this tenant (no concurrent double-refund).
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"refund:{tid}",))
    inserted = conn.execute(
        "INSERT INTO refund_executions "
        "(tenant_id, refund_reason, status, total_refund_paise, day39_evaluation_id) "
        "VALUES (%s, %s, 'pending', %s, %s) "
        "ON CONFLICT (tenant_id, refund_reason) DO NOTHING "
        "RETURNING tenant_id",
        (
            tid,
            refund_reason,
            int(total_refund_paise),
            str(day39_evaluation_id) if day39_evaluation_id is not None else None,
        ),
    ).fetchone()
    created = inserted is not None
    row = conn.execute(
        "SELECT * FROM refund_executions WHERE tenant_id = %s AND refund_reason = %s FOR UPDATE",
        (tid, refund_reason),
    ).fetchone()
    assert row is not None  # we either inserted it or it pre-existed
    return row, created


def set_status(conn: Any, tenant_id: UUID | str, refund_reason: str, status: str) -> None:
    """Move the row to ``status`` (intermediate transition; never 'completed' —
    use :func:`mark_completed`). Caller-owned tenant_connection transaction."""
    conn.execute(
        "UPDATE refund_executions SET status = %s, updated_at = now() "
        "WHERE tenant_id = %s AND refund_reason = %s",
        (status, str(tenant_id), refund_reason),
    )


def append_response(
    conn: Any, tenant_id: UUID | str, refund_reason: str, response: dict[str, Any]
) -> None:
    """Append one Razorpay/step response to the ``refund_responses`` JSONB array.

    Persisted BEFORE/AFTER each external call so a lost response on retry is
    visible (no double-refund). PII-free payload only (ids/amounts/status)."""
    conn.execute(
        "UPDATE refund_executions "
        "SET refund_responses = refund_responses || %s::jsonb, updated_at = now() "
        "WHERE tenant_id = %s AND refund_reason = %s",
        (json.dumps([response]), str(tenant_id), refund_reason),
    )


def mark_partial_failed(
    conn: Any,
    tenant_id: UUID | str,
    refund_reason: str,
    partial_refund_paise: int,
) -> None:
    """Terminal-ish failure state: refunds halted mid-stream. Fazal investigates;
    no auto-retry. Records how much was actually refunded for audit."""
    conn.execute(
        "UPDATE refund_executions "
        "SET status = 'partial_failed', partial_refund_paise = %s, updated_at = now() "
        "WHERE tenant_id = %s AND refund_reason = %s",
        (int(partial_refund_paise), str(tenant_id), refund_reason),
    )


def mark_completed(
    conn: Any,
    tenant_id: UUID | str,
    refund_reason: str,
    *,
    partial_refund_paise: int,
    notification_pending: bool,
) -> None:
    """Freeze the row as ``completed``. After this, the immutability trigger
    blocks any further UPDATE/DELETE (except the DSR purge session)."""
    conn.execute(
        "UPDATE refund_executions "
        "SET status = 'completed', partial_refund_paise = %s, "
        "    notification_pending = %s, completed_at = now(), updated_at = now() "
        "WHERE tenant_id = %s AND refund_reason = %s",
        (int(partial_refund_paise), bool(notification_pending), str(tenant_id), refund_reason),
    )


def get(tenant_id: UUID | str, refund_reason: str, *, conn: Any = None) -> dict[str, Any] | None:
    """Fetch the row (tenant-scoped). Opens a fresh tenant_connection when no
    caller conn is supplied."""
    if conn is not None:
        return conn.execute(
            "SELECT * FROM refund_executions WHERE tenant_id = %s AND refund_reason = %s",
            (str(tenant_id), refund_reason),
        ).fetchone()
    with tenant_connection(tenant_id) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM refund_executions WHERE tenant_id = %s AND refund_reason = %s",
            (str(tenant_id), refund_reason),
        )
        return cur.fetchone()


def anonymize_retain(conn: Any, tenant_id: UUID | str) -> int:
    """DSR anonymize-retain mode (the alternative to hard-delete): KEEP
    total_refund_paise + completed_at (Indian tax/accounting retention may require
    refund amount+date for 6-8 yrs even after a DPDP erasure) but SCRUB the
    Razorpay vendor detail in refund_responses. Runs on the service-role purge
    conn UNDER the ``orchestrator.dsr_purge_in_progress`` flag, so the completed-
    row immutability trigger permits the UPDATE. Returns rows scrubbed.

    Which path runs (hard-delete vs this) is a single config switch in dsr_purge —
    Fazal's/legal's retention ruling flips it without a refactor (Cowork escalation
    20260605T100800Z)."""
    cur = conn.execute(
        "UPDATE refund_executions SET refund_responses = '[]'::jsonb, "
        "notification_pending = false WHERE tenant_id = %s",
        (str(tenant_id),),
    )
    return cur.rowcount
