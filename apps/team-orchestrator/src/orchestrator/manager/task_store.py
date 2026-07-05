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
    "queued",  # VT-605 (mig 165): a later objective while one is already active for the tenant.
    "shadow",  # VT-606 round-3 (mig 167): a shadow-mode triage plan — content-recorded but NEVER
               # admitted/claimed/driven; excluded from TASK_ACTIVE below so it can never occupy
               # the tenant's one-active-task slot or block a real new_task's admission.
})
# VT-557: dead_letter is a terminal (retry budget spent) — but an OPERATOR-REDRIVABLE one
# (redrive_task resets it to 'planned'); the reaper never auto-retries a dead_letter row.
TASK_TERMINAL = frozenset({"completed", "failed", "cancelled", "dead_letter"})
TASK_NON_TERMINAL = TASK_STATUSES - TASK_TERMINAL
# VT-605: the subset of TASK_NON_TERMINAL that counts as "active" for the per-tenant one-active-task
# admission gate (mig 165's manager_tasks_one_active_per_tenant partial unique index) — 'queued'
# deliberately excluded (many queued tasks may coexist per tenant; only ONE may be active). 'shadow'
# ALSO excluded (VT-606 round-3): a shadow-mode plan has no driver and must never occupy the slot.
TASK_ACTIVE = TASK_NON_TERMINAL - {"queued", "shadow"}

# VT-605 (mig 165) columns; VT-606 (team-lead ruling round 2) wires the SETTER — the columns existed
# since mig 165 but nothing wrote them until the completion-verification checkpoint landed.
TERMINAL_OUTCOMES = frozenset(
    {"completed_with_effect", "completed_no_action", "failed", "escalated", "cancelled"}
)
OWNER_NOTIFICATION_STATUSES = frozenset(
    {"not_required", "pending", "accepted", "delivered", "failed"}
)

STEP_KINDS = frozenset({
    "specialist_dispatch", "effect", "clarification", "verification",
    "advisory_tool",  # VT-605 (mig 165): a Manager-held advisory tool call (agent/advisory_registry.py).
})
STEP_STATUSES = frozenset({
    "pending", "running", "waiting", "done", "failed", "skipped",
    "superseded",  # VT-605 (mig 165): orphaned by a revise_plan — no longer part of the current plan.
})
STEP_TERMINAL = frozenset({"done", "failed", "skipped", "superseded"})
STEP_NON_TERMINAL = STEP_STATUSES - STEP_TERMINAL

EVIDENCE_KINDS = frozenset({
    "campaign_plan", "agent_work_item", "pipeline_run",
    "pipeline_step",  # VT-605 (mig 165): a single pipeline_steps row (finer than a whole pipeline_run).
})


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
    terminal_outcome: str | None = None,
    owner_notification_status: str | None = None,
) -> bool:
    """Advance a task under the CAS guard. Returns True if the write applied, False on a
    CAS no-op (current state not in ``expected_from`` — a stale write, logged not raised).
    ``version`` bumps on every applied write; ``completed_at`` stamps on a terminal status;
    ``evidence_entry`` (a ``{kind, ref}`` dict) is appended to ``evidence_refs``.

    ``terminal_outcome`` / ``owner_notification_status`` (mig 165 columns; VT-606 wires the setter
    — nothing wrote them before the completion-verification checkpoint landed) are COALESCE-applied
    like ``current_step_id``: omitted (``None``) leaves the existing value untouched."""
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r}")
    if expected_from is not None:
        unknown = set(expected_from) - TASK_STATUSES
        if unknown:
            raise ValueError(f"unknown expected_from statuses {sorted(unknown)!r}")
    if terminal_outcome is not None and terminal_outcome not in TERMINAL_OUTCOMES:
        raise ValueError(f"unknown terminal_outcome {terminal_outcome!r}")
    if owner_notification_status is not None and owner_notification_status not in OWNER_NOTIFICATION_STATUSES:
        raise ValueError(f"unknown owner_notification_status {owner_notification_status!r}")
    terminal = status in TASK_TERMINAL
    sql = [
        "UPDATE manager_tasks SET status = %s, version = version + 1, updated_at = now(),",
        "completed_at = CASE WHEN %s THEN now() ELSE completed_at END,",
        "current_step_id = COALESCE(%s, current_step_id),",
        "terminal_outcome = COALESCE(%s, terminal_outcome),",
        "owner_notification_status = COALESCE(%s, owner_notification_status),",
        "evidence_refs = CASE WHEN %s::jsonb IS NULL THEN evidence_refs",
        "                     ELSE evidence_refs || %s::jsonb END",
        "WHERE tenant_id = %s AND id = %s",
    ]
    ev = Jsonb([redact(evidence_entry)]) if evidence_entry is not None else None
    params: list[Any] = [
        status, terminal,
        str(current_step_id) if current_step_id is not None else None,
        terminal_outcome,
        owner_notification_status,
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
            "idempotency_key, version, stall_metadata, created_at, updated_at, completed_at, "
            # VT-605 (mig 165): plan_revision / terminal_outcome / owner_notification_status —
            # plan_store.load_plan reads plan_revision to resolve the CURRENT step set.
            "plan_revision, terminal_outcome, owner_notification_status "
            "FROM manager_tasks WHERE tenant_id = %s AND id = %s",
            (str(tenant_id), str(task_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def has_active_task(tenant_id: UUID | str) -> bool:
    """VT-606 (triage seam) — does this tenant have ANY plan-store task in ``TASK_ACTIVE``. Mirrors
    the SAME check ``plan_store.create_plan`` runs internally for admission control (reused, not
    reimplemented, at the caller's own tenant-scoped read — no row lock needed here, this is an
    observational read for triage's ``has_active_task`` classification input, not an admission
    decision)."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM manager_tasks WHERE tenant_id = %s AND status = ANY(%s) LIMIT 1",
            (str(tenant_id), list(TASK_ACTIVE)),
        ).fetchone()
    return row is not None


def has_active_integration_step(tenant_id: UUID | str) -> bool:
    """VT-608 ruling 1 — the runner-gate DEFER check: is there an active plan-store task whose
    CURRENT step targets the integration_agent specialist? When True, the deterministic runner gate
    (``runner.py``'s ``maybe_resume_shopify_onboarding`` call site) defers to the loop — the loop
    owns this tenant's integration objective and reads the SAME ``tenant_integration_state`` truth,
    so both paths write through the same phase-state functions and the defer check is what prevents
    concurrent ownership (no dual-writer race). ``current_step_id`` (not just "any pending/running
    step") is the deliberate join key — a task can have other non-current steps; only the step the
    task is ACTUALLY on right now determines ownership."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM manager_tasks t "
            "JOIN manager_task_steps s ON s.id = t.current_step_id AND s.tenant_id = t.tenant_id "
            "WHERE t.tenant_id = %s AND t.status = ANY(%s) AND s.specialist = 'integration_agent' "
            "LIMIT 1",
            (str(tenant_id), list(TASK_ACTIVE)),
        ).fetchone()
    return row is not None


def find_task_id(tenant_id: UUID | str, idempotency_key: str) -> UUID | None:
    """Resolve a task's id from its ``(tenant, idempotency_key)`` — the live producer's run-keyed
    handle. A manager_task is minted at the delegation seam keyed on the run, so the later
    run-terminal seams (completed / paused / failed) look it up here instead of threading the id
    through graph state. RLS-scoped read; None when no such task exists (a conversational turn
    minted nothing)."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT id FROM manager_tasks WHERE tenant_id = %s AND idempotency_key = %s",
            (str(tenant_id), idempotency_key),
        ).fetchone()
    return _uuid(row) if row is not None else None


def find_task_by_source_ref(tenant_id: UUID | str, source_message_ref: str) -> UUID | None:
    """VT-605 — resolve a task by its ``source_message_ref`` pointer (run_id or message SID), the
    BROADER counterpart to ``find_task_id`` (which resolves by the narrower ``idempotency_key``).

    Two producers may key a task's IDENTITY (``idempotency_key``) DIFFERENTLY — the legacy
    ``task_producer`` keys on ``live_dispatch:{run_id}`` (VT-565); ``plan_store.create_plan`` keys
    on the inbound source-message SID (Package 2) — while both are expected to ALSO record the
    same human-meaningful pointer in ``source_message_ref``. Used as a CROSS-PRODUCER duplicate
    guard: before minting, check whether ANY producer already created a task for this reference,
    regardless of which idempotency scheme it used. Returns the OLDEST matching task (there should
    only ever be one; ``ORDER BY created_at`` is a defensive tie-break, not a real ambiguity).
    """
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT id FROM manager_tasks WHERE tenant_id = %s AND source_message_ref = %s "
            "ORDER BY created_at ASC LIMIT 1",
            (str(tenant_id), source_message_ref),
        ).fetchone()
    return _uuid(row) if row is not None else None


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
            # VT-605 (mig 165): plan_revision / specialist.
            "plan_revision, specialist, "
            "created_at, updated_at FROM manager_task_steps "
            "WHERE tenant_id = %s AND task_id = %s ORDER BY step_seq",
            (str(tenant_id), str(task_id)),
        ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "TASK_STATUSES", "TASK_TERMINAL", "TASK_NON_TERMINAL", "TASK_ACTIVE",
    "TERMINAL_OUTCOMES", "OWNER_NOTIFICATION_STATUSES",
    "STEP_KINDS", "STEP_STATUSES", "STEP_TERMINAL", "STEP_NON_TERMINAL", "EVIDENCE_KINDS",
    "create_task", "set_task_status", "get_task", "find_task_id", "find_task_by_source_ref",
    "has_active_task", "has_active_integration_step", "add_step", "set_step_status", "get_steps",
]
