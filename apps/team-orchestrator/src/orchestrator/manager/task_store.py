"""VT-525 (B2) — the Team-Manager task/step persistence spine.

Create/mint tasks (redelivery-safe via ``idempotency_key``), append an ordered step plan, and
advance both under a **CAS guard** that forbids a stale writer regressing a terminal state —
the ``coordinator._set_work_item_status`` VT-374 pattern, reused. Every free-text/JSONB field
is PII-redacted at write (``pii_redactor.redact``, CL-390) so no raw owner/customer text lands
at rest. All access is tenant-scoped through ``tenant_connection`` (RLS-enforced): the manager
writes as the tenant's service acting on its behalf.

This module is the PERSISTENCE half of B2. The manager decision loop that reasons over these
rows (accept/revise/next-specialist/clarify/escalate) is B3 (VT-526).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)

# ── State vocabularies (mirror the migration CHECK constraints) ──────────────
TASK_STATUSES = frozenset({
    "clarifying", "planned", "running", "waiting_owner", "blocked", "verifying",
    "completed", "failed", "cancelled", "dead_letter",
})
# VT-557: dead_letter is a terminal (retry budget spent) — but an OPERATOR-REDRIVABLE one
# (redrive_task resets it to 'planned'); the reaper never auto-retries a dead_letter row.
TASK_TERMINAL = frozenset({"completed", "failed", "cancelled", "dead_letter"})
TASK_NON_TERMINAL = TASK_STATUSES - TASK_TERMINAL

STEP_KINDS = frozenset({"specialist_dispatch", "effect", "clarification", "verification"})
STEP_STATUSES = frozenset({"pending", "running", "waiting", "done", "failed", "skipped"})
STEP_TERMINAL = frozenset({"done", "failed", "skipped"})
STEP_NON_TERMINAL = STEP_STATUSES - STEP_TERMINAL

EVIDENCE_KINDS = frozenset({"campaign_plan", "agent_work_item", "pipeline_run"})


def _uuid(row: Any) -> UUID:
    val = row["id"] if isinstance(row, dict) else row[0]
    return val if isinstance(val, UUID) else UUID(str(val))


# ── Tasks ────────────────────────────────────────────────────────────────────
def create_task(
    tenant_id: UUID | str,
    objective: dict[str, Any],
    *,
    acceptance_criteria: dict[str, Any] | None = None,
    source_message_ref: str | None = None,
    assigned_function: str | None = None,
    idempotency_key: str | None = None,
    status: str = "clarifying",
) -> UUID:
    """Mint a task (or return the existing one for a repeated ``idempotency_key``).

    The parent-row ``FOR UPDATE`` lock serializes concurrent minters (a redelivered webhook, a
    replay) INCLUDING across processes, so the check-then-insert is race-free; the unique index
    is the backstop. ``objective`` / ``acceptance_criteria`` are redacted before insert.
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r}")
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute("SELECT id FROM tenants WHERE id = %s FOR UPDATE", (str(tenant_id),)).fetchone()
        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT id FROM manager_tasks WHERE tenant_id = %s AND idempotency_key = %s",
                (str(tenant_id), idempotency_key),
            ).fetchone()
            if existing is not None:
                return _uuid(existing)
        row = conn.execute(
            "INSERT INTO manager_tasks "
            "(tenant_id, objective, acceptance_criteria, source_message_ref, assigned_function, "
            " idempotency_key, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                str(tenant_id),
                Jsonb(redact(objective)),
                Jsonb(redact(acceptance_criteria)) if acceptance_criteria is not None else None,
                source_message_ref,
                assigned_function,
                idempotency_key,
                status,
            ),
        ).fetchone()
    return _uuid(row)


def set_task_status(
    tenant_id: UUID | str,
    task_id: UUID | str,
    status: str,
    *,
    expected_from: tuple[str, ...] | None = None,
    current_step_id: UUID | str | None = None,
    evidence_entry: dict[str, Any] | None = None,
) -> bool:
    """Advance a task under the CAS guard. Returns True if the write applied, False on a
    CAS no-op (current state not in ``expected_from`` — a stale write, logged not raised).
    ``version`` bumps on every applied write; ``completed_at`` stamps on a terminal status;
    ``evidence_entry`` (a ``{kind, ref}`` dict) is appended to ``evidence_refs``."""
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r}")
    if expected_from is not None:
        unknown = set(expected_from) - TASK_STATUSES
        if unknown:
            raise ValueError(f"unknown expected_from statuses {sorted(unknown)!r}")
    terminal = status in TASK_TERMINAL
    sql = [
        "UPDATE manager_tasks SET status = %s, version = version + 1, updated_at = now(),",
        "completed_at = CASE WHEN %s THEN now() ELSE completed_at END,",
        "current_step_id = COALESCE(%s, current_step_id),",
        "evidence_refs = CASE WHEN %s::jsonb IS NULL THEN evidence_refs",
        "                     ELSE evidence_refs || %s::jsonb END",
        "WHERE tenant_id = %s AND id = %s",
    ]
    ev = Jsonb([redact(evidence_entry)]) if evidence_entry is not None else None
    params: list[Any] = [
        status, terminal,
        str(current_step_id) if current_step_id is not None else None,
        ev, ev,
        str(tenant_id), str(task_id),
    ]
    if expected_from is not None:
        sql.append("AND status = ANY(%s)")
        params.append(list(expected_from))
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(" ".join(sql), params)
        if cur.rowcount == 0:
            logger.warning(
                "manager_task status CAS no-op (task=%s -> %r; current state not in "
                "expected_from=%r) — stale write suppressed", task_id, status, expected_from,
            )
            return False
    return True


def redrive_task(tenant_id: UUID | str, task_id: UUID | str, *, conn: Any) -> bool:
    """VT-557 operator redrive — reset a dead_letter/blocked task to 'planned' for re-dispatch:
    attempt=0, next_retry_at=NULL, version+1. CAS-guarded to the redrivable states so a double
    redrive (or a completed/cancelled task) is a no-op → returns False. Runs on the caller's conn
    (the ops endpoint's service cursor) so the operator-audit row commits in the SAME txn."""
    cur = conn.execute(
        "UPDATE manager_tasks SET status = 'planned', attempt = 0, next_retry_at = NULL, "
        "    version = version + 1, updated_at = now() "
        "WHERE tenant_id = %s AND id = %s AND status IN ('dead_letter', 'blocked')",
        (str(tenant_id), str(task_id)),
    )
    return cur.rowcount > 0


def get_task(tenant_id: UUID | str, task_id: UUID | str) -> dict[str, Any] | None:
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT id, tenant_id, objective, acceptance_criteria, source_message_ref, "
            "assigned_function, policy_ref, status, current_step_id, evidence_refs, "
            "idempotency_key, version, stall_metadata, created_at, updated_at, completed_at "
            "FROM manager_tasks WHERE tenant_id = %s AND id = %s",
            (str(tenant_id), str(task_id)),
        ).fetchone()
    return dict(row) if row is not None else None


# ── Steps ──────────────────────────────────────────────────────────────────
def add_step(
    tenant_id: UUID | str,
    task_id: UUID | str,
    step_seq: int,
    kind: str,
    *,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
    detail: dict[str, Any] | None = None,
    status: str = "pending",
) -> UUID:
    """Append an ordered step to a task's plan. ``detail`` is redacted before insert."""
    if kind not in STEP_KINDS:
        raise ValueError(f"unknown step kind {kind!r}")
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status {status!r}")
    if evidence_kind is not None and evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence_kind {evidence_kind!r}")
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "INSERT INTO manager_task_steps "
            "(tenant_id, task_id, step_seq, kind, evidence_kind, evidence_ref, status, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                str(tenant_id), str(task_id), step_seq, kind, evidence_kind, evidence_ref, status,
                Jsonb(redact(detail)) if detail is not None else None,
            ),
        ).fetchone()
    return _uuid(row)


def set_step_status(
    tenant_id: UUID | str,
    step_id: UUID | str,
    status: str,
    *,
    expected_from: tuple[str, ...] | None = None,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
) -> bool:
    """Advance a step under the CAS guard (same semantics as ``set_task_status``)."""
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status {status!r}")
    if expected_from is not None:
        unknown = set(expected_from) - STEP_STATUSES
        if unknown:
            raise ValueError(f"unknown expected_from statuses {sorted(unknown)!r}")
    if evidence_kind is not None and evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence_kind {evidence_kind!r}")
    sql = [
        "UPDATE manager_task_steps SET status = %s, version = version + 1, updated_at = now(),",
        "evidence_kind = COALESCE(%s, evidence_kind),",
        "evidence_ref = COALESCE(%s, evidence_ref)",
        "WHERE tenant_id = %s AND id = %s",
    ]
    params: list[Any] = [status, evidence_kind, evidence_ref, str(tenant_id), str(step_id)]
    if expected_from is not None:
        sql.append("AND status = ANY(%s)")
        params.append(list(expected_from))
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(" ".join(sql), params)
        if cur.rowcount == 0:
            logger.warning(
                "manager_task_step status CAS no-op (step=%s -> %r; not in expected_from=%r) "
                "— stale write suppressed", step_id, status, expected_from,
            )
            return False
    return True


def get_steps(tenant_id: UUID | str, task_id: UUID | str) -> list[dict[str, Any]]:
    with tenant_connection(tenant_id) as conn:
        rows = conn.execute(
            "SELECT id, step_seq, kind, evidence_kind, evidence_ref, status, detail, version, "
            "created_at, updated_at FROM manager_task_steps "
            "WHERE tenant_id = %s AND task_id = %s ORDER BY step_seq",
            (str(tenant_id), str(task_id)),
        ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "TASK_STATUSES", "TASK_TERMINAL", "TASK_NON_TERMINAL",
    "STEP_KINDS", "STEP_STATUSES", "STEP_TERMINAL", "STEP_NON_TERMINAL", "EVIDENCE_KINDS",
    "create_task", "set_task_status", "get_task",
    "add_step", "set_step_status", "get_steps",
]
