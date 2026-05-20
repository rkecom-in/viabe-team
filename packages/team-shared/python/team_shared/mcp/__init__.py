"""MCP tool-contract framework (VT-39).

The contract every individual tool (VT-5.2 ... VT-5.13) implements
against. Provides:

  - ``MCPTool`` — abstract base with input/output schemas + ``execute``
  - ``ToolContext`` — typed bag carrying tenant identity, run_id, db_handle
  - ``ToolResult`` / ``ErrorEnvelope`` — uniform return shape
  - Validation: input-schema check BEFORE execute, output-schema check AFTER
  - Telemetry: ``pipeline_steps`` row per call (started/completed/failed)
  - ``run_tool_test`` test harness (one helper, every tool subtask uses it)

Tenant scoping (Pillar 3) is structural: ``ToolContext.tenant_id`` is
set by the orchestrator at the dispatch boundary. Tools READ it; tools
cannot accept ``tenant_id`` as an input field — the registry refuses
to register a tool whose input schema declares one. Tested.
"""

from team_shared.mcp.framework import (
    ErrorCode,
    ErrorEnvelope,
    MCPTool,
    ToolContext,
    ToolResult,
    ToolStatus,
)
from team_shared.mcp.telemetry import PipelineStepsTelemetry, TelemetrySink
from team_shared.mcp.test_harness import (
    ToolTestFixture,
    run_tool_test,
)

__all__ = [
    "ErrorCode",
    "ErrorEnvelope",
    "MCPTool",
    "PipelineStepsTelemetry",
    "TelemetrySink",
    "ToolContext",
    "ToolResult",
    "ToolStatus",
    "ToolTestFixture",
    "run_tool_test",
]
