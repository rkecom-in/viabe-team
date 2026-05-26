"""VT-179 envelope: ``mcp_tool_call`` (MCP-framework tool invocation)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class McpToolCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    tool_args: dict[str, Any]


class McpToolCallOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_result: Any
    cost_paise: int = 0
    duration_ms: int = 0


class McpToolCallEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "mcp_tool_call"

    input_envelope: McpToolCallInput
    output_envelope: McpToolCallOutput


__all__ = [
    "McpToolCallInput",
    "McpToolCallOutput",
    "McpToolCallEnvelope",
]
