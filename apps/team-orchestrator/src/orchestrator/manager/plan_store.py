"""VT-605 (Loop Package 2) — the EXECUTABLE plan store: typed APIs around ``task_store``.

``task_store`` (VT-525) owns the low-level task/step CAS primitives. This module is the RICHER
layer the durable manager workflow (VT-606) will drive: ``create_plan`` / ``load_plan`` /
``revise_plan`` / ``claim_next_step`` / ``complete_step``, operating on the STRICT ``ManagerPlan`` /
``PlanStep`` models (``manager/plan_models.py``) instead of raw dicts.

A "plan" has NO separate storage — it is the ordered collection of ``manager_task_steps`` rows at a
task's CURRENT ``plan_revision`` (mig 165), reassembled by ``load_plan``. This mirrors the codebase's
existing "no unified effects table" discipline (mig 151's own comment) — REUSE, don't invent a
second source of truth for what is already the steps table.

Atomicity: ``create_plan`` inserts the task row AND every step row in ONE transaction (the task-row
``FOR UPDATE`` lock — the SAME serialization discipline ``task_store.create_task`` already uses —
makes the idempotency check-then-insert race-free too). ``revise_plan`` supersedes-then-appends in
ONE transaction. Nothing here is ever "half persisted."

CAS discipline: ``claim_next_step`` / ``complete_step`` reuse ``task_store``'s own CAS primitives
(``set_step_status``'s ``expected_from`` guard) — a stale worker's write is suppressed (logged, not
raised), never silently regresses or double-advances a step. ``revise_plan`` takes an EXPLICIT
``expected_plan_revision`` and raises ``PlanRevisionConflict`` when a concurrent reviser already
moved the task past it — the multi-row equivalent of the same discipline.

Audit: every plan create / revise / step claim / step terminal emits ONE ``tm_audit_log`` row via
``emit_tm_audit`` IN THE SAME TRANSACTION (the "action layer, fail-closed" mode — an un-auditable
plan mutation must not commit, mirroring the VT-460 rails analog). Structured summaries + counts
only — NEVER the raw ``situation`` / ``desired_outcome`` prose (CL-390).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.manager import task_store
from orchestrator.manager.plan_models import EvidenceRef, ManagerPlan, PlanStep
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)

StepTerminalStatus = Literal["done", "failed", "skipped"]


class PlanRevisionConflict(RuntimeError):
    """``revise_plan`` was called against a STALE ``expected_plan_revision`` — a concurrent reviser
    already moved the task's plan forward. The multi-row CAS equivalent of ``set_task_status``'s
    ``expected_from`` no-op, surfaced as a raise (not a bool) because there is no sensible "did it
    apply" return value once the caller's whole ``new_plan`` was built against a revision that no
    longer exists."""

    def __init__(self, *, task_id: UUID | str, expected: int, actual: int) -> None:
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"revise_plan CAS conflict: task={task_id} expected plan_revision={expected}, "
            f"actual={actual} (a concurrent reviser moved the plan first)"
        )


def _uuid(row: Any, key: str = "id", idx: int = 0) -> UUID:
    val = row[key] if isinstance(row, dict) else row[idx]
    return val if isinstance(val, UUID) else UUID(str(val))


def _step_detail(step: PlanStep) -> dict[str, Any]:
    """The redacted JSONB ``detail`` blob for one step — free text goes through ``pii_redactor.redact``
    (CL-390); structured fields (kind/specialist/step_seq) live on their OWN typed columns, not here."""
    # redact() is typed Any -> Any (it recurses over arbitrary JSON shapes); a dict in always
    # yields a dict out (pii_redactor.redact's own contract: "dict: keys preserved").
    return cast(
        "dict[str, Any]",
        redact(
            {
                "situation": step.situation,
                "desired_outcome": step.desired_outcome,
                "acceptance_criteria": step.acceptance_criteria,
                "allowed_effect_classes": step.allowed_effect_classes,
            }
        ),
    )


def _insert_step(conn: Any, tenant_id: UUID | str, task_id: UUID | str, plan_revision: int, step: PlanStep) -> UUID:
    row = conn.execute(
        "INSERT INTO manager_task_steps "
        "(tenant_id, task_id, step_seq, plan_revision, kind, specialist, status, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING id",
        (
            str(tenant_id), str(task_id), step.step_seq, plan_revision,
            step.kind, step.specialist, Jsonb(_step_detail(step)),
        ),
    ).fetchone()
    return _uuid(row)


# ── create_plan ──────────────────────────────────────────────────────────────
def create_plan(
    tenant_id: UUID | str,
    plan: ManagerPlan,
    *,
    source_message_sid: str,
    assigned_function: str | None = None,
) -> UUID:
    """Create the task + persist the FULL plan atomically. Returns the task id.

    Idempotent per ``(tenant_id, source_message_sid)`` — ``source_message_sid`` IS the task's
    ``idempotency_key`` (execution-plan §2: "Use source message SID as the task idempotency key").
    A duplicate inbound event (redelivered webhook / DBOS step retry re-attempting the same call)
    returns the EXISTING task id; it never creates a second task or a second plan.

    Admission control (Package 2: "Admit one active objective-bearing task per tenant. Additional
    objectives become queued"): if the tenant already has an ACTIVE plan-store task
    (``task_store.TASK_ACTIVE`` — anything non-terminal and non-queued), the new task is created
    with status ``'queued'`` instead of ``'planned'``. Enforced at the APPLICATION level, under the
    SAME ``tenants`` row ``FOR UPDATE`` lock ``task_store.create_task`` uses (race-free against
    concurrent ``create_plan`` calls for the same tenant) — deliberately NOT a table-wide DB
    constraint: the EXISTING legacy ``task_producer`` (VT-565) mints one ephemeral task PER RUN and
    legitimately has multiple concurrently-``running`` tasks per tenant (see mig 165's comment
    block). This is a NEW plan-store-level policy, not a retroactive system-wide invariant.
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute("SELECT id FROM tenants WHERE id = %s FOR UPDATE", (str(tenant_id),)).fetchone()

        existing = conn.execute(
            "SELECT id FROM manager_tasks WHERE tenant_id = %s AND idempotency_key = %s",
            (str(tenant_id), source_message_sid),
        ).fetchone()
        if existing is not None:
            logger.info(
                "plan_store.create_plan: idempotent replay (tenant=%s sid_prefix=%s) — "
                "returning existing task, no duplicate plan created",
                tenant_id, source_message_sid[:12],
            )
            return _uuid(existing)

        active = conn.execute(
            "SELECT 1 FROM manager_tasks WHERE tenant_id = %s AND status = ANY(%s) LIMIT 1",
            (str(tenant_id), list(task_store.TASK_ACTIVE)),
        ).fetchone()
        status = "queued" if active is not None else "planned"

        task_row = conn.execute(
            "INSERT INTO manager_tasks "
            "(tenant_id, objective, acceptance_criteria, source_message_ref, assigned_function, "
            " idempotency_key, status, plan_revision) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                str(tenant_id),
                Jsonb(redact({"objective": plan.objective, "schema_version": plan.schema_version})),
                Jsonb(redact({"acceptance_criteria": plan.acceptance_criteria})),
                source_message_sid,
                assigned_function,
                source_message_sid,
                status,
                plan.plan_revision,
            ),
        ).fetchone()
        task_id = _uuid(task_row)

        first_step_id: UUID | None = None
        for step in plan.steps:
            step_id = _insert_step(conn, tenant_id, task_id, plan.plan_revision, step)
            if step.step_seq == 1:
                first_step_id = step_id

        conn.execute(
            "UPDATE manager_tasks SET current_step_id = %s WHERE tenant_id = %s AND id = %s",
            (str(first_step_id) if first_step_id else None, str(tenant_id), str(task_id)),
        )

        emit_tm_audit(
            event_layer="does",
            event_kind="plan_created",
            actor="team_manager",
            tenant_id=tenant_id,
            summary=f"plan created: {len(plan.steps)} step(s), status={status!r}",
            decision={
                "task_id": str(task_id),
                "plan_revision": plan.plan_revision,
                "step_count": len(plan.steps),
                "admission_status": status,
            },
            conn=conn,
        )
    return task_id


# ── load_plan ────────────────────────────────────────────────────────────────
def load_plan(tenant_id: UUID | str, task_id: UUID | str) -> ManagerPlan | None:
    """Reassemble the task's CURRENT-revision ``ManagerPlan`` from ``manager_tasks`` +
    ``manager_task_steps``. Returns ``None`` if no such task exists. Survives a process restart by
    construction — this is a pure read over durable state; re-instantiating the store and calling
    ``load_plan`` again reconstructs the IDENTICAL plan (the restart-survival acceptance test)."""
    task = task_store.get_task(tenant_id, task_id)
    if task is None:
        return None

    with tenant_connection(tenant_id) as conn:
        rows = conn.execute(
            "SELECT step_seq, kind, specialist, detail FROM manager_task_steps "
            "WHERE tenant_id = %s AND task_id = %s AND plan_revision = %s "
            "  AND status != 'superseded' "
            "ORDER BY step_seq",
            (str(tenant_id), str(task_id), task["plan_revision"]),
        ).fetchall()

    steps: list[PlanStep] = []
    for r in rows:
        detail = (r["detail"] if isinstance(r, dict) else r[3]) or {}
        step_seq = r["step_seq"] if isinstance(r, dict) else r[0]
        kind = r["kind"] if isinstance(r, dict) else r[1]
        specialist = r["specialist"] if isinstance(r, dict) else r[2]
        steps.append(
            PlanStep(
                step_seq=step_seq,
                kind=kind,
                specialist=specialist,
                situation=detail.get("situation", ""),
                desired_outcome=detail.get("desired_outcome", ""),
                acceptance_criteria=detail.get("acceptance_criteria", []),
                allowed_effect_classes=detail.get("allowed_effect_classes", []),
            )
        )

    objective_blob = task.get("objective") or {}
    acceptance_blob = task.get("acceptance_criteria") or {}
    return ManagerPlan(
        objective=objective_blob.get("objective", ""),
        acceptance_criteria=acceptance_blob.get("acceptance_criteria", []),
        steps=steps,
        plan_revision=task["plan_revision"],
    )


# ── revise_plan ──────────────────────────────────────────────────────────────
def revise_plan(
    tenant_id: UUID | str,
    task_id: UUID | str,
    new_plan: ManagerPlan,
    *,
    expected_plan_revision: int,
) -> ManagerPlan:
    """Supersede-not-edit revision (Package 2: "Revisions never edit completed history. Mark
    pending old-revision steps superseded, increment plan_revision, and append replacement steps").

    CAS: ``expected_plan_revision`` MUST match the task's CURRENT ``plan_revision`` (read under the
    task row's ``FOR UPDATE`` lock) or this raises ``PlanRevisionConflict`` — a stale caller (built
    ``new_plan`` against a revision another worker already moved past) can never blindly overwrite
    the live plan. Only PENDING steps of the old revision are marked ``superseded``; any step
    already terminal (done/failed/skipped) is REAL history and is left untouched.

    Returns the new plan (a copy of ``new_plan`` with ``plan_revision`` set to the incremented
    value actually persisted).
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        task_row = conn.execute(
            "SELECT plan_revision FROM manager_tasks WHERE tenant_id = %s AND id = %s FOR UPDATE",
            (str(tenant_id), str(task_id)),
        ).fetchone()
        if task_row is None:
            raise ValueError(f"revise_plan: no such task {task_id!r} for tenant {tenant_id!r}")
        current_revision = task_row["plan_revision"] if isinstance(task_row, dict) else task_row[0]
        if current_revision != expected_plan_revision:
            raise PlanRevisionConflict(
                task_id=task_id, expected=expected_plan_revision, actual=current_revision
            )

        new_revision = current_revision + 1

        superseded = conn.execute(
            "UPDATE manager_task_steps SET status = 'superseded', version = version + 1, "
            "    updated_at = now() "
            "WHERE tenant_id = %s AND task_id = %s AND plan_revision = %s AND status = ANY(%s)",
            (str(tenant_id), str(task_id), current_revision, list(task_store.STEP_NON_TERMINAL)),
        )
        superseded_count = superseded.rowcount

        first_step_id: UUID | None = None
        for step in new_plan.steps:
            step_id = _insert_step(conn, tenant_id, task_id, new_revision, step)
            if step.step_seq == 1:
                first_step_id = step_id

        conn.execute(
            "UPDATE manager_tasks SET plan_revision = %s, objective = %s, acceptance_criteria = %s, "
            "    current_step_id = %s, version = version + 1, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (
                new_revision,
                Jsonb(redact({"objective": new_plan.objective, "schema_version": new_plan.schema_version})),
                Jsonb(redact({"acceptance_criteria": new_plan.acceptance_criteria})),
                str(first_step_id) if first_step_id else None,
                str(tenant_id), str(task_id),
            ),
        )

        emit_tm_audit(
            event_layer="decides",
            event_kind="plan_revised",
            actor="team_manager",
            tenant_id=tenant_id,
            summary=(
                f"plan revised: revision {current_revision} -> {new_revision}, "
                f"{superseded_count} step(s) superseded, {len(new_plan.steps)} new step(s)"
            ),
            decision={
                "task_id": str(task_id),
                "old_plan_revision": current_revision,
                "new_plan_revision": new_revision,
                "superseded_step_count": superseded_count,
                "new_step_count": len(new_plan.steps),
            },
            conn=conn,
        )
    return new_plan.model_copy(update={"plan_revision": new_revision})


# ── claim_next_step ──────────────────────────────────────────────────────────
def claim_next_step(tenant_id: UUID | str, task_id: UUID | str) -> dict[str, Any] | None:
    """Atomically find + claim the task's next PENDING step (lowest ``step_seq`` in the CURRENT
    plan revision) — CAS-guarded (``status='pending' -> 'running'``) so two concurrent claimers can
    never both pick the same step. Returns the claimed step as a dict, or ``None`` when there is no
    claimable step (the plan is exhausted, or every remaining step is already running/terminal).
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        task_row = conn.execute(
            "SELECT plan_revision FROM manager_tasks WHERE tenant_id = %s AND id = %s FOR UPDATE",
            (str(tenant_id), str(task_id)),
        ).fetchone()
        if task_row is None:
            return None
        revision = task_row["plan_revision"] if isinstance(task_row, dict) else task_row[0]

        candidate = conn.execute(
            "SELECT id, step_seq, kind, specialist, detail FROM manager_task_steps "
            "WHERE tenant_id = %s AND task_id = %s AND plan_revision = %s AND status = 'pending' "
            "ORDER BY step_seq LIMIT 1 FOR UPDATE",
            (str(tenant_id), str(task_id), revision),
        ).fetchone()
        if candidate is None:
            return None
        step_id = _uuid(candidate)
        step_seq = candidate["step_seq"] if isinstance(candidate, dict) else candidate[1]
        kind = candidate["kind"] if isinstance(candidate, dict) else candidate[2]
        specialist = candidate["specialist"] if isinstance(candidate, dict) else candidate[3]
        detail = (candidate["detail"] if isinstance(candidate, dict) else candidate[4]) or {}

        cur = conn.execute(
            "UPDATE manager_task_steps SET status = 'running', version = version + 1, "
            "    updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = 'pending'",
            (str(tenant_id), str(step_id)),
        )
        if cur.rowcount == 0:
            logger.warning(
                "plan_store.claim_next_step: CAS no-op (step=%s already claimed) — stale claim suppressed",
                step_id,
            )
            return None

        conn.execute(
            "UPDATE manager_tasks SET current_step_id = %s, status = 'running', "
            "    version = version + 1, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (str(step_id), str(tenant_id), str(task_id)),
        )

        emit_tm_audit(
            event_layer="does",
            event_kind="step_claimed",
            actor="team_manager",
            tenant_id=tenant_id,
            summary=f"step claimed: seq={step_seq} kind={kind!r}",
            decision={
                "task_id": str(task_id), "step_id": str(step_id),
                "step_seq": step_seq, "kind": kind, "specialist": specialist,
            },
            conn=conn,
        )
    return {
        "step_id": step_id,
        "step_seq": step_seq,
        "kind": kind,
        "specialist": specialist,
        "situation": detail.get("situation", ""),
        "desired_outcome": detail.get("desired_outcome", ""),
        "acceptance_criteria": detail.get("acceptance_criteria", []),
        "allowed_effect_classes": detail.get("allowed_effect_classes", []),
    }


# ── complete_step ────────────────────────────────────────────────────────────
def complete_step(
    tenant_id: UUID | str,
    step_id: UUID | str,
    status: StepTerminalStatus,
    *,
    evidence: EvidenceRef | None = None,
    expected_from: tuple[str, ...] = ("running",),
) -> bool:
    """CAS-guarded terminal transition for ONE step. Delegates to ``task_store.set_step_status``
    (the SAME CAS primitive) — reused, not reimplemented. Returns ``True`` if applied, ``False`` on
    a CAS no-op (a stale/regressed write suppressed, never raised — mirrors ``set_step_status``).
    """
    evidence_kind = evidence.kind if evidence is not None else None
    evidence_ref = evidence.ref if evidence is not None else None
    applied = task_store.set_step_status(
        tenant_id, step_id, status,
        expected_from=expected_from, evidence_kind=evidence_kind, evidence_ref=evidence_ref,
    )
    # Audit the terminal regardless of CAS outcome (a suppressed stale write is itself an
    # observable event — "a worker tried to complete a step that had already moved on").
    emit_tm_audit(
        event_layer="does",
        event_kind="step_completed" if applied else "step_completed_cas_noop",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"step {status} (applied={applied})",
        decision={
            "step_id": str(step_id), "status": status, "applied": applied,
            "evidence_kind": evidence_kind,
        },
    )
    return applied


__all__ = [
    "PlanRevisionConflict",
    "StepTerminalStatus",
    "claim_next_step",
    "complete_step",
    "create_plan",
    "load_plan",
    "revise_plan",
]
