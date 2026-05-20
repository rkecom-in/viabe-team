"""Test harness for MCP tools (VT-39).

Every tool subtask (VT-5.2 ... VT-5.13) consumes ``run_tool_test``
against its fixtures. A CI grep enforces the import so future tools
cannot ship without exercising the harness.

Single helper: ``run_tool_test(tool_cls, fixtures)``. Runs each fixture
through ``tool.call(ctx, raw_inputs)`` and asserts:

  - positive fixture → ``ToolResult.status == OK`` + data shape matches
    output_schema
  - negative fixture → ``ToolResult.status != OK`` AND ``error`` is
    populated with the expected ``ErrorCode``

The harness also supplies a ``RecordingTelemetry`` sink for tests that
want to assert lifecycle events without writing to a DB.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from team_shared.mcp.framework import (
    ErrorCode,
    MCPTool,
    ToolContext,
    ToolResult,
    ToolStatus,
)


@dataclass
class ToolTestFixture:
    """One fixture for ``run_tool_test``.

    ``name`` is a descriptive label (shown in assertion failures).
    ``raw_inputs`` is the dict passed to ``tool.call``. ``ctx`` is the
    ToolContext to invoke under (typically a per-fixture one with a
    distinct tenant_id so cross-tenant tests can land in the same
    suite).

    Set ``expect_status=OK`` for positive fixtures; otherwise set the
    expected non-OK status AND ``expect_error_code`` to lock the error
    classification.
    """

    name: str
    raw_inputs: dict[str, Any]
    ctx: ToolContext
    expect_status: ToolStatus = ToolStatus.OK
    expect_error_code: ErrorCode | None = None
    expect_data_predicate: Callable[[dict[str, Any]], bool] | None = None


@dataclass
class _RecordedEvent:
    """One telemetry event captured by ``RecordingTelemetry``."""

    kind: str  # 'started' | 'completed' | 'failed'
    payload: dict[str, Any]


class RecordingTelemetry:
    """An in-memory ``TelemetrySink`` for tests. Records every event so
    assertions can read them back. Implements ``TelemetrySink`` Protocol
    structurally (duck typing — Protocol checked at the call site)."""

    def __init__(self) -> None:
        self.events: list[_RecordedEvent] = []
        self._next_handle = 0

    def tool_call_started(
        self,
        *,
        tool_name: str,
        tenant_id: UUID,
        run_id: UUID,
        input_hash: str,
        is_llm_backed: bool,
    ) -> str:
        self._next_handle += 1
        handle = f"rec-{self._next_handle}"
        self.events.append(
            _RecordedEvent(
                kind="started",
                payload={
                    "handle": handle,
                    "tool_name": tool_name,
                    "tenant_id": str(tenant_id),
                    "run_id": str(run_id),
                    "input_hash": input_hash,
                    "is_llm_backed": is_llm_backed,
                },
            )
        )
        return handle

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
        self.events.append(
            _RecordedEvent(
                kind="completed",
                payload={
                    "handle": handle,
                    "status": status,
                    "tokens_used": tokens_used,
                    "cost_paise": cost_paise,
                    "latency_ms": latency_ms,
                    "model_used": model_used,
                },
            )
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
        self.events.append(
            _RecordedEvent(
                kind="failed",
                payload={
                    "handle": handle,
                    "error_code": error_code,
                    "error_message": error_message,
                    "latency_ms": latency_ms,
                    "model_used": model_used,
                },
            )
        )


@dataclass
class HarnessReport:
    """One report row per fixture run, returned by ``run_tool_test``."""

    fixture_name: str
    passed: bool
    result: ToolResult
    failure_reason: str | None = None


@contextmanager
def _no_op_db_factory_ctx(_tenant_id: Any) -> Any:
    """Default db_handle for fixtures that don't touch the DB."""

    class _NoConn:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(
                "No DB access from the harness's default db_handle — pass a "
                "real factory in ToolContext if the tool reads the DB."
            )

    yield _NoConn()


def no_op_db_factory(tenant_id: Any) -> Any:
    """A no-op tenant-connection factory the harness uses by default.
    Tools that need real DB access supply their own factory via
    ``ToolContext.db_handle`` in their fixtures."""
    return _no_op_db_factory_ctx(tenant_id)


def run_tool_test(
    tool_cls: type[MCPTool[Any, Any]],
    fixtures: list[ToolTestFixture],
) -> list[HarnessReport]:
    """Run each fixture through ``tool_cls().call`` and assert.

    Returns a list of reports, one per fixture. A failing report's
    ``failure_reason`` carries a short string. Callers typically wrap
    this in ``assert all(r.passed for r in reports)`` after iterating
    to print every failure.
    """
    tool = tool_cls()
    reports: list[HarnessReport] = []
    for fix in fixtures:
        result = tool.call(fix.ctx, fix.raw_inputs)
        passed = True
        reason: str | None = None

        if result.status is not fix.expect_status:
            passed = False
            reason = (
                f"status {result.status.value} != expected "
                f"{fix.expect_status.value}"
            )
        elif fix.expect_status is ToolStatus.OK:
            if result.data is None:
                passed, reason = False, "OK status but data is None"
            elif fix.expect_data_predicate is not None and not (
                fix.expect_data_predicate(result.data)
            ):
                passed, reason = False, "data predicate returned False"
        else:
            # Non-OK fixture
            if result.error is None:
                passed, reason = False, "non-OK status but error is None"
            elif (
                fix.expect_error_code is not None
                and result.error.code is not fix.expect_error_code
            ):
                passed = False
                reason = (
                    f"error.code {result.error.code.value} != expected "
                    f"{fix.expect_error_code.value}"
                )

        reports.append(
            HarnessReport(
                fixture_name=fix.name,
                passed=passed,
                result=result,
                failure_reason=reason,
            )
        )
    return reports


__all__ = [
    "HarnessReport",
    "RecordingTelemetry",
    "ToolTestFixture",
    "no_op_db_factory",
    "run_tool_test",
]
