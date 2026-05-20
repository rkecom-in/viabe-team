"""Telemetry sink for MCP tool calls (VT-39).

Brief item 4: "pipeline_log event on every execute(): tool_call_started
(name + tenant_id + run_id + input hash), tool_call_completed (status +
tokens + cost_paise + latency_ms), tool_call_failed (on error)."

On main today, ``pipeline_log`` is the ``pipeline_steps`` table
(migrations/006, VT-12.2). The three "events" land as ONE row per tool
call, lifecycle-encoded via the table's started_at + ended_at +
output_envelope + error_envelope columns — same pattern
``orchestrator.error_router`` uses for its decision rows.

The ``TelemetrySink`` Protocol is the framework's contract; the
``PipelineStepsTelemetry`` implementation is the production sink (calls
into ``orchestrator.db.tenant_connection`` for RLS-scoped INSERTs).
Tests inject a ``RecordingTelemetry`` (in test_harness.py) to assert
events without touching the DB.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class TelemetrySink(Protocol):
    """The framework's telemetry contract. Implementations write to
    ``pipeline_steps`` (production) or to memory (tests)."""

    def tool_call_started(
        self,
        *,
        tool_name: str,
        tenant_id: UUID,
        run_id: UUID,
        input_hash: str,
        is_llm_backed: bool,
    ) -> str:
        """Record the start of a tool call. Returns a sink-local handle
        the caller passes back on completed/failed so the sink can
        update the same row."""
        ...

    def tool_call_completed(
        self,
        *,
        handle: str,
        tenant_id: UUID,
        status: str,
        tokens_used: int,
        cost_paise: int,
        latency_ms: int,
        model_used: str | None = None,
    ) -> None:
        """Record successful completion."""
        ...

    def tool_call_failed(
        self,
        *,
        handle: str,
        tenant_id: UUID,
        error_code: str,
        error_message: str,
        latency_ms: int,
        model_used: str | None = None,
    ) -> None:
        """Record a failure (any non-OK status)."""
        ...


class PipelineStepsTelemetry:
    """Production sink — writes to the ``pipeline_steps`` table via
    ``tenant_connection`` (RLS enforced per CL-122).

    ONE row per tool call: ``step_kind = 'tool_call'`` with
    ``input_envelope = {name, input_hash, is_llm_backed}``. The
    completed/failed updates set ``ended_at``, ``output_envelope``,
    ``cost_paise``, ``duration_ms``, and (on failure) ``error_envelope``.

    LLM-backed tools include ``model_used`` in the output_envelope's
    metadata field — required for cost-attribution audit per brief
    item 4.
    """

    def __init__(
        self,
        tenant_connection_factory: Any,
    ) -> None:
        """``tenant_connection_factory`` matches the signature of
        ``orchestrator.db.tenant_connection`` — accepts a tenant_id,
        returns a context-managed psycopg connection."""
        self._tc = tenant_connection_factory

    def tool_call_started(
        self,
        *,
        tool_name: str,
        tenant_id: UUID,
        run_id: UUID,
        input_hash: str,
        is_llm_backed: bool,
    ) -> str:
        from psycopg.types.json import Jsonb

        with self._tc(tenant_id) as conn, conn.transaction():
            next_index_row = conn.execute(
                "SELECT COALESCE(MAX(step_index), 0) + 1 AS next "
                "FROM pipeline_steps WHERE run_id = %s",
                (str(run_id),),
            ).fetchone()
            next_index = int(next_index_row["next"])
            row = conn.execute(
                """
                INSERT INTO pipeline_steps
                    (run_id, tenant_id, step_index, step_kind, input_envelope)
                VALUES (%s, %s, %s, 'tool_call', %s)
                RETURNING id
                """,
                (
                    str(run_id),
                    str(tenant_id),
                    next_index,
                    Jsonb(
                        {
                            "name": tool_name,
                            "input_hash": input_hash,
                            "is_llm_backed": is_llm_backed,
                        }
                    ),
                ),
            ).fetchone()
            return str(row["id"])

    def tool_call_completed(
        self,
        *,
        handle: str,
        tenant_id: UUID,
        status: str,
        tokens_used: int,
        cost_paise: int,
        latency_ms: int,
        model_used: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        envelope: dict[str, Any] = {
            "status": status,
            "tokens_used": tokens_used,
        }
        if model_used is not None:
            envelope["model_used"] = model_used

        with self._tc(tenant_id) as conn:
            conn.execute(
                """
                UPDATE pipeline_steps SET
                    output_envelope = %s,
                    cost_paise = %s,
                    duration_ms = %s,
                    ended_at = now()
                WHERE id = %s
                """,
                (Jsonb(envelope), cost_paise, latency_ms, handle),
            )

    def tool_call_failed(
        self,
        *,
        handle: str,
        tenant_id: UUID,
        error_code: str,
        error_message: str,
        latency_ms: int,
        model_used: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        envelope: dict[str, Any] = {
            "code": error_code,
            "message": error_message,
        }
        if model_used is not None:
            envelope["model_used"] = model_used

        with self._tc(tenant_id) as conn:
            conn.execute(
                """
                UPDATE pipeline_steps SET
                    error_envelope = %s,
                    duration_ms = %s,
                    ended_at = now()
                WHERE id = %s
                """,
                (Jsonb(envelope), latency_ms, handle),
            )


__all__ = ["PipelineStepsTelemetry", "TelemetrySink"]
