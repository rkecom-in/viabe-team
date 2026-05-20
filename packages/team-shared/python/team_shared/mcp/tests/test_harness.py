"""VT-39 — test-harness self-test.

Runs the harness against a synthetic tool with one positive and one
negative fixture. Confirms the harness reports correctly on both.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel, Field  # noqa: E402

from team_shared.mcp import (  # noqa: E402
    ErrorCode,
    MCPTool,
    ToolContext,
    ToolStatus,
    run_tool_test,
)
from team_shared.mcp.test_harness import (  # noqa: E402
    RecordingTelemetry,
    ToolTestFixture,
    no_op_db_factory,
)


class _AddInput(BaseModel):
    a: int = Field(..., ge=0)
    b: int = Field(..., ge=0)


class _AddOutput(BaseModel):
    sum: int


class _AddTool(MCPTool[_AddInput, _AddOutput]):
    name = "add"
    description = "Add two non-negative integers."
    input_schema = _AddInput
    output_schema = _AddOutput

    def execute(self, ctx: ToolContext, inputs: _AddInput) -> _AddOutput:
        return _AddOutput(sum=inputs.a + inputs.b)


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        agent_id="test-agent",
        parent_tool_call_id=None,
        cost_budget_remaining_paise=1000,
        wallclock_remaining_ms=60_000,
        db_handle=no_op_db_factory,
    )


def test_run_tool_test_passes_on_positive_and_negative_fixtures():
    """Standard usage: one positive fixture + one negative fixture
    (wrong tenant via malformed input). Harness returns a report per
    fixture."""
    positive = ToolTestFixture(
        name="positive: 2 + 3 = 5",
        raw_inputs={"a": 2, "b": 3},
        ctx=_ctx(),
        expect_status=ToolStatus.OK,
        expect_data_predicate=lambda d: d["sum"] == 5,
    )
    negative = ToolTestFixture(
        name="negative: b=-1 rejected by schema",
        raw_inputs={"a": 1, "b": -1},
        ctx=_ctx(),
        expect_status=ToolStatus.ERROR,
        expect_error_code=ErrorCode.INVALID_INPUT,
    )
    reports = run_tool_test(_AddTool, [positive, negative])
    assert len(reports) == 2
    assert all(r.passed for r in reports), [
        (r.fixture_name, r.failure_reason) for r in reports if not r.passed
    ]


def test_run_tool_test_surfaces_assertion_failure():
    """A fixture whose expectation doesn't match → harness reports
    passed=False with a readable reason. Used by suites that iterate
    + print all failures rather than failing on the first."""
    wrong_expectation = ToolTestFixture(
        name="positive that ACTUALLY fails the predicate",
        raw_inputs={"a": 2, "b": 3},
        ctx=_ctx(),
        expect_status=ToolStatus.OK,
        expect_data_predicate=lambda d: d["sum"] == 99,  # wrong
    )
    reports = run_tool_test(_AddTool, [wrong_expectation])
    assert reports[0].passed is False
    assert reports[0].failure_reason is not None
    assert "predicate" in reports[0].failure_reason


def test_recording_telemetry_captures_started_completed_failed():
    """The RecordingTelemetry sink records every event with handle
    correlation. Used by tests that assert lifecycle events without
    touching the DB."""
    sink = RecordingTelemetry()
    tenant_id = uuid4()
    run_id = uuid4()
    handle = sink.tool_call_started(
        tool_name="add",
        tenant_id=tenant_id,
        run_id=run_id,
        input_hash="abc123",
        is_llm_backed=False,
    )
    sink.tool_call_completed(
        handle=handle,
        tenant_id=tenant_id,
        status="ok",
        tokens_used=0,
        cost_paise=0,
        latency_ms=42,
    )
    sink.tool_call_failed(
        handle=handle,
        tenant_id=tenant_id,
        error_code="execution_error",
        error_message="downstream timeout",
        latency_ms=99,
    )
    kinds = [e.kind for e in sink.events]
    assert kinds == ["started", "completed", "failed"]
    assert sink.events[0].payload["handle"] == handle
    assert sink.events[1].payload["latency_ms"] == 42
    assert sink.events[2].payload["error_code"] == "execution_error"
