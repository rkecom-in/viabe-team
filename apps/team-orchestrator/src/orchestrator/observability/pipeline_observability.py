"""VT-180 ``write_step`` writer — load-bearing pipeline_steps writer.

Per design-doc §3 + CL-417 canonical schema discipline. Atomic INSERT
pipeline_steps + UPDATE pipeline_runs.{step_count, total_cost_paise} in
a single ``tenant_connection()`` transaction. Envelope validation via
VT-179 ``STEP_KIND_REGISTRY`` (hard-fail on unregistered step_kind,
soft-fail on schema drift). SQLite buffer fallback when prod DB is
unavailable. Monotonic ``step_seq`` via the ``SELECT MAX(step_seq) FOR
UPDATE`` pattern matching ``error_router._log_decision`` +
``collapse._emit_campaign_plan_emitted`` on-main.

Per CL-416 retention discipline: this module has ZERO delete / expire /
aggregate-drop paths. DSR-purge (VT-185) is the sole deletion path.

VT-379: ``pipeline_steps.error`` is redacted at write (it was the one
free-text column that bypassed the redactor), the customer-name registry
is threaded through every redaction call (it was ``name_registry=None``
everywhere — VT-374 audit), and ``write_redacted_step_row`` is the shared
redacting INSERT path for the legacy direct writers
(``error_router._log_decision``, ``sales_recovery._emit_self_evaluate_gate``,
``collapse.record_terminal_verdict``) whose row shapes pre-date
``write_step``.

Cross-references:
- VT-179 envelope registry: ``orchestrator.observability.envelopes``
- VT-104 PII redaction: ``orchestrator.observability.pii.redact_for_log``
- VT-170 name registry: ``orchestrator.privacy.customer_registry``
- VT-102 soft-fail pattern: ``orchestrator.observability.log``
- VT-187 canonical schema: ``migrations/025_pipeline_observability_normalize.sql``
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from orchestrator.db.tenant_connection import tenant_connection
from orchestrator.observability.envelopes import envelope_for
from orchestrator.observability.pii import redact_for_log

logger = logging.getLogger(__name__)


# Default buffer location: alongside the app root, hidden by leading dot.
# Override via VIABE_OBSERVABILITY_BUFFER_PATH for tests / canaries.
_BUFFER_PATH_DEFAULT = (
    Path(__file__).resolve().parents[4] / ".observability_buffer.db"
)


def _buffer_path() -> Path:
    override = os.environ.get("VIABE_OBSERVABILITY_BUFFER_PATH")
    return Path(override) if override else _BUFFER_PATH_DEFAULT


def _ensure_buffer_schema(path: Path) -> None:
    """Create SQLite buffer table if missing. Idempotent.

    Schema mirrors pipeline_steps canonical columns (VT-187) plus a
    ``buffered_at_utc`` timestamp for FIFO flush ordering. JSONB columns
    are stored as TEXT (json.dumps); reconstructed on flush.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buffered_steps (
              rowid              INTEGER PRIMARY KEY AUTOINCREMENT,
              buffered_at_utc    TEXT NOT NULL,
              run_id             TEXT NOT NULL,
              tenant_id          TEXT NOT NULL,
              step_kind          TEXT NOT NULL,
              step_name          TEXT,
              parent_step_id     TEXT,
              input_envelope     TEXT,
              output_envelope    TEXT,
              status             TEXT NOT NULL,
              decision_rationale TEXT,
              tool_calls         TEXT,
              error              TEXT,
              cost_paise         INTEGER NOT NULL DEFAULT 0,
              model_used         TEXT,
              tokens_input       INTEGER,
              tokens_output      INTEGER,
              override_id        TEXT,
              paused_ms          INTEGER
            )
            """
        )
        # VT-374 additive columns — a buffer file created pre-mig-131 lacks them
        # (CREATE IF NOT EXISTS skips); patch in place so the INSERT never breaks.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(buffered_steps)")}
        for col, decl in (("override_id", "TEXT"), ("paused_ms", "INTEGER")):
            if col not in existing:
                conn.execute(f"ALTER TABLE buffered_steps ADD COLUMN {col} {decl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS buffered_steps_order_idx "
            "ON buffered_steps (buffered_at_utc, rowid)"
        )


def _registry_for_tenant(tenant_id: UUID | str) -> Callable[[str], bool] | None:
    """Build the tenant's customer-name registry for write-time redaction.

    VT-379 posture decision — FAIL-SOFT, documented: when the registry
    build fails (customers read error, pool unavailable, ...) this
    degrades to PATTERN-ONLY redaction with a structured warning log.
    Rationale: observability writers sit on live pipeline hot paths
    (``record_intervention``'s never-raise contract, the error_router /
    gate / collapse best-effort writers) — a registry outage must not
    kill a live pipeline, and pattern redaction (phones, email, PAN,
    Aadhaar, IFSC, GST, CC, long bodies) still runs unconditionally.

    CONTRAST — the VT-374 ops API (``api/ops_run_control.py``) is
    fail-CLOSED on the same build: there a human is submitting free text
    whose persistence is the request itself, so a silent registry no-op
    would deliberately store under-redacted PII and refusing the request
    is cheap. Here the row is collateral telemetry; dropping the write or
    raising into the pipeline is the worse privacy/availability trade.
    """
    try:
        # Lazy import: keeps the registry (and its DB wrapper chain) off
        # this module's import path; cached per tenant after first build.
        from orchestrator.privacy.customer_registry import make_name_registry

        return make_name_registry(str(tenant_id))
    except Exception:  # noqa: BLE001 — fail-soft by contract (see docstring)
        logger.warning(
            "pipeline_observability: name-registry build failed; "
            "falling back to pattern-only redaction",
            extra={"tenant_id": str(tenant_id), "redaction_mode": "pattern_only"},
            exc_info=True,
        )
        return None


def write_step(
    *,
    step_kind: str,
    run_id: UUID,
    tenant_id: UUID,
    step_name: str | None = None,
    input_envelope: dict[str, Any],
    output_envelope: dict[str, Any] | None = None,
    status: str = "completed",
    parent_step_id: UUID | None = None,
    decision_rationale: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
    cost_paise: int = 0,
    model_used: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    override_id: UUID | None = None,
    paused_ms: int | None = None,
    name_registry: Callable[[str], bool] | None = None,
) -> None:
    """Write one pipeline_steps row + atomically update pipeline_runs cumulatives.

    VT-374: ``override_id`` / ``paused_ms`` are structural run-control columns
    (mig 131) — populated where a controllable-seam wrapper executed; NOT part
    of the typed envelope (no envelope schema change), so they skip step 2.

    Per CL-417: canonical per-field columns populated directly; envelopes
    carry payload-specific extras only.
    Per CL-19 / VT-179: envelope validated against STEP_KIND_REGISTRY;
    unregistered step_kind = hard-fail (caller bug), malformed envelope =
    soft-fail (write proceeds with ``error.payload_validation_failed`` flag).
    Per CL-104: input/output envelopes flow through VT-104 redactor before
    JSONB serialization.
    Per VT-379: ``error`` flows through the same redactor (it was the one
    free-text column written raw), and redaction consults the tenant's
    customer-name registry — pass ``name_registry`` to inject one, or
    leave ``None`` and it is built lazily from ``tenant_id`` (fail-soft
    to pattern-only; see ``_registry_for_tenant``).
    Per CL-416: NO delete / expire / aggregate-drop paths.

    Raises ``EnvelopeNotRegistered`` (from VT-179) if ``step_kind`` has no
    registered envelope — the caller has a bug.
    """
    # 1. Envelope class lookup (hard-fail on unregistered)
    EnvClass = envelope_for(step_kind)

    # 2. Envelope shape validation (soft-fail on schema drift)
    #    step_seq=0 dummy: the real value is derived inside the transaction;
    #    StepEnvelope.step_seq is unconstrained int, so 0 is valid for shape
    #    validation (VT-179 base.py).
    error_dict: dict[str, Any] = dict(error) if error else {}
    try:
        EnvClass(
            run_id=run_id,
            tenant_id=tenant_id,
            step_seq=0,
            step_name=step_name,
            parent_step_id=parent_step_id,
            status=status,  # type: ignore[arg-type]
            decision_rationale=decision_rationale,
            model_used=model_used,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tool_calls=tool_calls,
            started_at=datetime.now(timezone.utc),
            error=error_dict if error_dict else None,
            input_envelope=input_envelope,  # type: ignore[arg-type]
            output_envelope=output_envelope,  # type: ignore[arg-type]
        )
    except ValidationError as ve:
        error_dict["payload_validation_failed"] = True
        error_dict["payload_validation_details"] = [
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in ve.errors()
        ]
        logger.warning(
            "write_step envelope validation soft-fail",
            extra={
                "step_kind": step_kind,
                "run_id": str(run_id),
                "tenant_id": str(tenant_id),
                "error_count": len(ve.errors()),
            },
        )

    # 3. PII redaction (CL-104; VT-379 — error column + tenant name registry).
    #    Registry built lazily when the caller did not inject one; fail-soft
    #    to pattern-only (see _registry_for_tenant for the posture decision).
    registry = (
        name_registry if name_registry is not None else _registry_for_tenant(tenant_id)
    )
    input_envelope_safe = (
        redact_for_log(input_envelope, name_registry=registry)
        if input_envelope
        else {}
    )
    output_envelope_safe = (
        redact_for_log(output_envelope, name_registry=registry)
        if output_envelope
        else None
    )
    error_safe: dict[str, Any] | None = (
        redact_for_log(error_dict, name_registry=registry) if error_dict else None
    )
    # decision_rationale carries agent think_text the callbacks pre-redact PATTERN-ONLY
    # (no registry in their context) — re-redact here with the tenant registry so a bare
    # customer name in short reasoning snippets cannot persist (the VT-379 review catch).
    rationale_safe: str | None = (
        cast(str, redact_for_log(decision_rationale, name_registry=registry))
        if decision_rationale is not None
        else None
    )

    # 4. Atomic write: try prod DB; on connection failure, buffer locally
    try:
        _do_db_write(
            step_kind=step_kind,
            run_id=run_id,
            tenant_id=tenant_id,
            step_name=step_name,
            input_envelope_safe=input_envelope_safe,
            output_envelope_safe=output_envelope_safe,
            status=status,
            parent_step_id=parent_step_id,
            decision_rationale=rationale_safe,
            tool_calls=tool_calls,
            error=error_safe,
            cost_paise=cost_paise,
            model_used=model_used,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            override_id=override_id,
            paused_ms=paused_ms,
        )
    except (psycopg.OperationalError, psycopg.errors.ConnectionFailure) as exc:
        _append_to_buffer(
            step_kind=step_kind,
            run_id=run_id,
            tenant_id=tenant_id,
            step_name=step_name,
            input_envelope_safe=input_envelope_safe,
            output_envelope_safe=output_envelope_safe,
            status=status,
            parent_step_id=parent_step_id,
            decision_rationale=rationale_safe,
            tool_calls=tool_calls,
            error=error_safe,
            cost_paise=cost_paise,
            model_used=model_used,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            override_id=override_id,
            paused_ms=paused_ms,
        )
        logger.warning(
            "write_step prod DB unavailable; buffered locally",
            extra={
                "step_kind": step_kind,
                "run_id": str(run_id),
                "exc": repr(exc),
            },
        )


def record_intervention(
    tenant_id: UUID | str,
    run_id: UUID | str,
    *,
    workflow_kind: str,
    step_name: str,
    override_id: UUID | None = None,
    paused_ms: int | None = None,
    action: str,
) -> None:
    """VT-374 B1 — record one run-control intervention on the run's timeline.

    One pipeline_steps row: ``step_kind='run_control_intervention'``,
    ``step_name='<workflow_kind>:<step_name>'``, with the mig-131 structural
    ``override_id`` / ``paused_ms`` COLUMNS populated (this helper is their
    writer — the columns are dead without it). The envelope carries IDs/enums
    ONLY (action + workflow_kind + step_name; CL-390 — never free text); the
    vtr_step_timeline view shows this kind keys-only by default and the
    columns carry the data.

    ``action``: 'released' (a hold ended, the step proceeded), 'held' (the
    seam parked the run instead of proceeding), 'override_consumed' (a
    step_overrides row was claimed; pass ``override_id``).

    NEVER raises — callers sit at live controllable seams (durable workflow
    bodies, graph nodes); a timeline-write failure must not alter control
    semantics (F9 spirit). Failures log loudly instead.
    """
    try:
        write_step(
            step_kind="run_control_intervention",
            run_id=run_id if isinstance(run_id, UUID) else UUID(str(run_id)),
            tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
            step_name=f"{workflow_kind}:{step_name}",
            input_envelope={
                "action": action,
                "workflow_kind": workflow_kind,
                "step_name": step_name,
            },
            override_id=override_id,
            paused_ms=paused_ms,
        )
    except Exception:  # noqa: BLE001 — observability must not break a control seam
        logger.warning(
            "record_intervention failed (kind=%s step=%s run=%s action=%s) — "
            "control semantics unchanged",
            workflow_kind,
            step_name,
            run_id,
            action,
            exc_info=True,
        )


def write_redacted_step_row(
    *,
    run_id: UUID | str,
    tenant_id: UUID | str,
    step_kind: str,
    status: str = "completed",
    step_name: str | None = None,
    output_envelope: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    decision_rationale: str | None = None,
    name_registry: Callable[[str], bool] | None = None,
) -> None:
    """VT-379 — shared redacting INSERT for the legacy direct writers.

    Consumers: ``error_router._log_decision`` (step_kind='error'),
    ``sales_recovery._emit_self_evaluate_gate`` ('self_evaluate_gate'),
    ``collapse.record_terminal_verdict`` ('campaign_plan_emitted').

    Why not ``write_step``: those writers' row shapes pre-date it and
    genuinely don't fit —
    (a) their envelope payloads do not validate against the VT-179
        registry classes (e.g. the gate envelope carries
        ``attempt_number``/``outcome``, not ``verdict``/``reasons``), so
        write_step's soft-fail would inject ``payload_validation_*``
        keys into the ``error`` column — a row-semantics change;
    (b) they deliberately do NOT bump ``pipeline_runs.step_count`` /
        ``total_cost_paise`` and write no ``input_envelope``.
    This helper preserves each writer's exact row semantics (step_kind /
    step_name / MAX(step_seq)+1 linkage / column set) while centralizing
    redaction: ``output_envelope``, ``error`` and ``decision_rationale``
    all flow through the VT-104 redactor with the tenant name registry
    (built lazily, fail-soft to pattern-only — ``_registry_for_tenant``).
    NO raw INSERT of envelope/error free text remains at the call sites.

    Raises on DB failure — each caller keeps its own best-effort
    catch-and-log contract (observability must not break recovery).
    RLS enforced via ``tenant_connection`` (CL-122 / Pillar 3).
    """
    registry = (
        name_registry if name_registry is not None else _registry_for_tenant(tenant_id)
    )
    output_safe: dict[str, Any] | None = (
        redact_for_log(output_envelope, name_registry=registry)
        if output_envelope
        else None
    )
    error_safe: dict[str, Any] | None = (
        redact_for_log(error, name_registry=registry) if error else None
    )
    rationale_safe: str | None = (
        cast(str, redact_for_log(decision_rationale, name_registry=registry))
        if decision_rationale is not None
        else None
    )

    with tenant_connection(tenant_id) as conn, conn.transaction():
        # Same monotonic step_seq pattern the three writers used on-main
        # (dict_row factory configured on the pool; cast at the seam).
        raw = conn.execute(
            "SELECT COALESCE(MAX(step_seq), 0) + 1 AS next "
            "FROM pipeline_steps WHERE run_id = %s",
            (str(run_id),),
        ).fetchone()
        next_seq = int(cast("dict[str, Any]", raw)["next"])
        conn.execute(
            """
            INSERT INTO pipeline_steps
                (run_id, tenant_id, step_seq, step_kind, step_name,
                 output_envelope, error, decision_rationale, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(run_id),
                str(tenant_id),
                next_seq,
                step_kind,
                step_name,
                Jsonb(output_safe) if output_safe else None,
                Jsonb(error_safe) if error_safe else None,
                rationale_safe,
                status,
            ),
        )


def _do_db_write(
    *,
    step_kind: str,
    run_id: UUID,
    tenant_id: UUID,
    step_name: str | None,
    input_envelope_safe: dict[str, Any],
    output_envelope_safe: dict[str, Any] | None,
    status: str,
    parent_step_id: UUID | None,
    decision_rationale: str | None,
    tool_calls: list[dict[str, Any]] | None,
    error: dict[str, Any] | None,
    cost_paise: int,
    model_used: str | None,
    tokens_input: int | None,
    tokens_output: int | None,
    override_id: UUID | None = None,
    paused_ms: int | None = None,
) -> None:
    """Single-transaction INSERT pipeline_steps + UPDATE pipeline_runs.

    Concurrency strategy: lock the pipeline_runs row FOR UPDATE first,
    then SELECT MAX(step_seq). The run-row lock serializes concurrent
    writers on the same run_id (Postgres rejects ``FOR UPDATE`` on a
    query with aggregate functions, so we cannot lock the steps query
    itself). This gives a strict total order over writes per run while
    keeping inter-run writes parallel.
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute(
            "SELECT id FROM pipeline_runs WHERE id = %s FOR UPDATE",
            (str(run_id),),
        ).fetchone()
        raw = conn.execute(
            "SELECT COALESCE(MAX(step_seq), 0) + 1 AS next "
            "FROM pipeline_steps WHERE run_id = %s",
            (str(run_id),),
        ).fetchone()
        next_seq = int(cast("dict[str, Any]", raw)["next"])

        conn.execute(
            """
            INSERT INTO pipeline_steps (
              run_id, tenant_id, step_seq, step_kind, step_name,
              parent_step_id, input_envelope, output_envelope, status,
              decision_rationale, tool_calls, error, cost_paise,
              model_used, tokens_input, tokens_output, override_id,
              paused_ms, started_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, now()
            )
            """,
            (
                str(run_id),
                str(tenant_id),
                next_seq,
                step_kind,
                step_name,
                str(parent_step_id) if parent_step_id else None,
                Jsonb(input_envelope_safe),
                Jsonb(output_envelope_safe) if output_envelope_safe else None,
                status,
                decision_rationale,
                Jsonb(tool_calls) if tool_calls else None,
                Jsonb(error) if error else None,
                cost_paise,
                model_used,
                tokens_input,
                tokens_output,
                str(override_id) if override_id else None,
                paused_ms,
            ),
        )

        conn.execute(
            """
            UPDATE pipeline_runs
               SET step_count       = COALESCE(step_count, 0) + 1,
                   total_cost_paise = COALESCE(total_cost_paise, 0) + %s
             WHERE id = %s
            """,
            (cost_paise, str(run_id)),
        )


def _append_to_buffer(
    *,
    step_kind: str,
    run_id: UUID,
    tenant_id: UUID,
    step_name: str | None,
    input_envelope_safe: dict[str, Any],
    output_envelope_safe: dict[str, Any] | None,
    status: str,
    parent_step_id: UUID | None,
    decision_rationale: str | None,
    tool_calls: list[dict[str, Any]] | None,
    error: dict[str, Any] | None,
    cost_paise: int,
    model_used: str | None,
    tokens_input: int | None,
    tokens_output: int | None,
    override_id: UUID | None = None,
    paused_ms: int | None = None,
) -> None:
    """Append one row to local SQLite buffer. Idempotent schema-create."""
    path = _buffer_path()
    _ensure_buffer_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO buffered_steps (
              buffered_at_utc, run_id, tenant_id, step_kind, step_name,
              parent_step_id, input_envelope, output_envelope, status,
              decision_rationale, tool_calls, error, cost_paise,
              model_used, tokens_input, tokens_output, override_id, paused_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(run_id),
                str(tenant_id),
                step_kind,
                step_name,
                str(parent_step_id) if parent_step_id else None,
                json.dumps(input_envelope_safe),
                json.dumps(output_envelope_safe) if output_envelope_safe else None,
                status,
                decision_rationale,
                json.dumps(tool_calls) if tool_calls else None,
                json.dumps(error) if error else None,
                cost_paise,
                model_used,
                tokens_input,
                tokens_output,
                str(override_id) if override_id else None,
                paused_ms,
            ),
        )


def _flush_buffer() -> int:
    """Drain SQLite buffer into prod pipeline_steps via tenant_connection.

    Returns count of rows successfully flushed. FIFO via buffered_at_utc +
    rowid (stable tie-breaker). Per row: opens its own transaction; on
    successful prod commit, DELETEs from SQLite. Re-raises on prod DB
    still unavailable so the caller can decide retry policy.

    Safe to call repeatedly. When the buffer file does not exist or is
    empty, returns 0 without raising.
    """
    path = _buffer_path()
    if not path.exists():
        return 0
    # Patch a pre-mig-131 buffer in place (adds override_id/paused_ms) so the
    # SELECT below never breaks on an old file. Idempotent.
    _ensure_buffer_schema(path)
    flushed = 0
    with sqlite3.connect(path) as sqlite_conn:
        sqlite_conn.row_factory = sqlite3.Row
        rows = sqlite_conn.execute(
            """
            SELECT rowid, buffered_at_utc, run_id, tenant_id, step_kind,
                   step_name, parent_step_id, input_envelope,
                   output_envelope, status, decision_rationale,
                   tool_calls, error, cost_paise, model_used,
                   tokens_input, tokens_output, override_id, paused_ms
              FROM buffered_steps
             ORDER BY buffered_at_utc, rowid
            """
        ).fetchall()
        for row in rows:
            run_id = UUID(row["run_id"])
            tenant_id = UUID(row["tenant_id"])
            input_envelope = json.loads(row["input_envelope"]) if row["input_envelope"] else {}
            output_envelope = (
                json.loads(row["output_envelope"]) if row["output_envelope"] else None
            )
            tool_calls = json.loads(row["tool_calls"]) if row["tool_calls"] else None
            error = json.loads(row["error"]) if row["error"] else None

            _do_db_write(
                step_kind=row["step_kind"],
                run_id=run_id,
                tenant_id=tenant_id,
                step_name=row["step_name"],
                input_envelope_safe=input_envelope,
                output_envelope_safe=output_envelope,
                status=row["status"],
                parent_step_id=UUID(row["parent_step_id"])
                if row["parent_step_id"]
                else None,
                decision_rationale=row["decision_rationale"],
                tool_calls=tool_calls,
                error=error,
                cost_paise=row["cost_paise"],
                model_used=row["model_used"],
                tokens_input=row["tokens_input"],
                tokens_output=row["tokens_output"],
                override_id=UUID(row["override_id"]) if row["override_id"] else None,
                paused_ms=row["paused_ms"],
            )
            sqlite_conn.execute(
                "DELETE FROM buffered_steps WHERE rowid = ?", (row["rowid"],)
            )
            sqlite_conn.commit()
            flushed += 1
    return flushed


__all__ = [
    "record_intervention",
    "write_redacted_step_row",
    "write_step",
    "_flush_buffer",
    "_buffer_path",
    "_ensure_buffer_schema",
]
