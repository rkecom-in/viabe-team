"""VT-39 framework tests — contract, validation, tenant-scope guard."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel, Field  # noqa: E402

from team_shared.mcp import (  # noqa: E402
    ErrorCode,
    MCPTool,
    ToolContext,
    ToolStatus,
)
from team_shared.mcp.framework import _RegistryRejection  # noqa: E402
from team_shared.mcp.test_harness import (  # noqa: E402
    no_op_db_factory,
)


# ---------- helpers -----------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        agent_id="sales_recovery",
        parent_tool_call_id=None,
        cost_budget_remaining_paise=10_000,
        wallclock_remaining_ms=60_000,
        db_handle=no_op_db_factory,
    )


# ---------- synthetic tool: legal, well-behaved -------------------------------


class _EchoInput(BaseModel):
    message: str = Field(..., min_length=1)


class _EchoOutput(BaseModel):
    echoed: str


class _EchoTool(MCPTool[_EchoInput, _EchoOutput]):
    name = "echo"
    description = "Echo a message back uppercased."
    input_schema = _EchoInput
    output_schema = _EchoOutput

    def execute(self, ctx: ToolContext, inputs: _EchoInput) -> _EchoOutput:
        return _EchoOutput(echoed=inputs.message.upper())


def test_synthetic_tool_call_lifecycle_ok():
    """Happy path: validated input → execute → validated output → OK ToolResult."""
    tool = _EchoTool()
    result = tool.call(_ctx(), {"message": "hello"})
    assert result.status is ToolStatus.OK
    assert result.data == {"echoed": "HELLO"}
    assert result.error is None
    assert result.latency_ms >= 0


def test_invalid_input_returns_envelope_without_calling_execute():
    """If input-schema validation fails, ``execute`` is NEVER reached."""

    class _CountingEcho(_EchoTool):
        name = "echo_counting"
        executes_called: int = 0

        def execute(self, ctx: ToolContext, inputs: _EchoInput) -> _EchoOutput:
            type(self).executes_called += 1
            return _EchoOutput(echoed=inputs.message.upper())

    tool = _CountingEcho()
    result = tool.call(_ctx(), {"message": ""})  # min_length=1 violation
    assert result.status is ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert _CountingEcho.executes_called == 0


# ---------- invalid output → agent never sees it -----------------------------


class _LiarOutput(BaseModel):
    answer: int


class _LiarTool(MCPTool[_EchoInput, _LiarOutput]):
    name = "liar"
    description = "Returns malformed output."
    input_schema = _EchoInput
    output_schema = _LiarOutput

    def execute(self, ctx: ToolContext, inputs: _EchoInput) -> Any:
        # WRONG SHAPE — returns a dict missing the 'answer' field.
        return {"not_answer": "garbage"}  # type: ignore[return-value]


def test_invalid_output_returns_envelope_agent_never_sees_data():
    """Output-validation failure → ToolResult.error with INVALID_OUTPUT;
    data is None so the agent never sees the malformed payload."""
    tool = _LiarTool()
    result = tool.call(_ctx(), {"message": "hi"})
    assert result.status is ToolStatus.ERROR
    assert result.data is None
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_OUTPUT


# ---------- execute exception trap -------------------------------------------


class _ExplodingTool(MCPTool[_EchoInput, _EchoOutput]):
    name = "exploder"
    description = "Raises during execute."
    input_schema = _EchoInput
    output_schema = _EchoOutput

    def execute(self, ctx: ToolContext, inputs: _EchoInput) -> _EchoOutput:
        raise RuntimeError("boom — connection refused")


def test_execute_exception_traps_to_execution_error():
    """An exception from execute becomes ErrorCode.EXECUTION_ERROR;
    framework never lets a raw exception propagate to the agent."""
    tool = _ExplodingTool()
    result = tool.call(_ctx(), {"message": "x"})
    assert result.status is ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_ERROR
    assert "boom" in result.error.message


# ---------- Pillar 3: tenant_id in input → registry rejection ----------------


def test_tool_declaring_tenant_id_in_input_schema_is_refused():
    """A tool whose input_schema declares ``tenant_id`` MUST be refused
    at subclass-definition time. Pillar 3: tenant boundary is set by
    the orchestrator, never by the agent (CL-122 / CL-202)."""

    class _BadInput(BaseModel):
        tenant_id: UUID  # agent-supplied tenant boundary → forbidden
        message: str

    class _BadOutput(BaseModel):
        ok: bool

    with pytest.raises(_RegistryRejection, match="tenant_id"):

        class _AgentSetsTenantTool(MCPTool[_BadInput, _BadOutput]):
            name = "bad_tenant"
            description = "Tries to accept tenant_id as input — refused."
            input_schema = _BadInput
            output_schema = _BadOutput

            def execute(
                self, ctx: ToolContext, inputs: _BadInput
            ) -> _BadOutput:
                return _BadOutput(ok=True)


# ---------- LLM-backed flag default ------------------------------------------


def test_is_llm_backed_defaults_false():
    assert _EchoTool.is_llm_backed() is False


class _LLMBackedTool(MCPTool[_EchoInput, _EchoOutput]):
    name = "llm_backed_synthetic"
    description = "Synthetic LLM-backed tool for tests."
    input_schema = _EchoInput
    output_schema = _EchoOutput

    @classmethod
    def is_llm_backed(cls) -> bool:
        # Rationale: a synthetic test of the LLM-backed flag — not a
        # real production tool. Real LLM-backed tools cite the rationale
        # doc at this override site.
        return True

    def execute(self, ctx: ToolContext, inputs: _EchoInput) -> _EchoOutput:
        return _EchoOutput(echoed=inputs.message.upper())


def test_llm_backed_override_is_visible():
    assert _LLMBackedTool.is_llm_backed() is True


# ---------- ErrorEnvelope truncation -----------------------------------------


def test_error_envelope_truncates_overflow_message():
    """The framework caps ``message`` to 200 chars even if a tool author
    constructs a longer one — last-line defence against accidental
    PII / huge payloads landing in pipeline_steps.error_envelope."""
    from team_shared.mcp.framework import ErrorEnvelope

    env = ErrorEnvelope(code=ErrorCode.EXECUTION_ERROR, message="x" * 500)
    assert len(env.message) <= 200
    assert env.message.endswith("...")


# ---------- Missing class attribute → TypeError at subclass creation ---------


def test_missing_class_attrs_fail_loud():
    """A concrete tool without ``name`` / ``input_schema`` etc. fails
    at class-definition time — far better than a runtime AttributeError
    inside the dispatch."""

    class _IncompleteInput(BaseModel):
        x: int

    class _IncompleteOutput(BaseModel):
        y: int

    with pytest.raises(TypeError, match="name"):

        class _MissingName(MCPTool[_IncompleteInput, _IncompleteOutput]):
            description = "Missing the name."
            input_schema = _IncompleteInput
            output_schema = _IncompleteOutput

            def execute(
                self, ctx: ToolContext, inputs: _IncompleteInput
            ) -> _IncompleteOutput:
                return _IncompleteOutput(y=inputs.x)
