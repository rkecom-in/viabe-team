"""Observability layer (VT-101 → VT-104 → VT-171 Logfire migration).

Logfire integration + canonical PII redactor + ``pipeline_log`` writer +
cost dashboard + reasoning trace. The ``run_id`` plumbed through the
orchestrator (UUID type, surfaced in ``context_builder``, ``collapse``,
``runner``) serves as the cross-span correlation key — Pillar 8's "one
namespace" satisfied by reuse, not by generation.
"""

from orchestrator.privacy.pii_redactor import redact
from orchestrator.observability.reasoning_trace import (
    capture_agent_reasoning_step,
    capture_tool_call_args,
    capture_tool_call_result,
)
from orchestrator.observability.cost_dashboard import (
    detect_cost_anomalies,
    format_cost_breakdown_for_ops,
    get_tenant_cost,
    get_tenant_unit_economics,
    get_workspace_cost_summary,
    runaway_alert_candidates,
)
from orchestrator.observability.logfire import (
    configure_logfire,
    format_run_id_footer,
    get_project_name,
    instrument_orchestrator,
    is_enabled,
    request_with_trace_headers,
    shutdown,
    trace_run,
    trace_run_sync,
    traceable_node,
    traceable_tool,
    traced_node,
    traced_tool,
)
from orchestrator.observability.log import log_event, purge_pipeline_log_older_than
from orchestrator.observability.pii import (
    redact_for_langsmith,
    redact_for_log,
    redact_for_otel_span,
)
from orchestrator.observability.query import (
    query_errors_recent,
    query_event_type,
    query_run,
    query_tenant_recent,
)
from orchestrator.observability.types import (
    CostAnomaly,
    CostRunaway,
    PipelineLogEvent,
    TenantCostBreakdown,
    TenantUnitEconomics,
    WorkspaceCostSummary,
)

__all__ = [
    "CostAnomaly",
    "CostRunaway",
    "PipelineLogEvent",
    "TenantCostBreakdown",
    "TenantUnitEconomics",
    "WorkspaceCostSummary",
    "capture_agent_reasoning_step",
    "capture_tool_call_args",
    "capture_tool_call_result",
    "configure_logfire",
    "detect_cost_anomalies",
    "format_cost_breakdown_for_ops",
    "format_run_id_footer",
    "get_project_name",
    "get_tenant_cost",
    "get_tenant_unit_economics",
    "get_workspace_cost_summary",
    "instrument_orchestrator",
    "is_enabled",
    "log_event",
    "purge_pipeline_log_older_than",
    "query_errors_recent",
    "query_event_type",
    "query_run",
    "query_tenant_recent",
    "redact",
    "redact_for_langsmith",
    "redact_for_log",
    "redact_for_otel_span",
    "request_with_trace_headers",
    "runaway_alert_candidates",
    "shutdown",
    "trace_run",
    "trace_run_sync",
    "traceable_node",
    "traceable_tool",
    "traced_node",
    "traced_tool",
]
