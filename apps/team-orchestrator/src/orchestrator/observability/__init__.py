"""Observability layer (VT-101).

LangSmith integration + PII redaction. The ``run_id`` already plumbed through
the orchestrator (UUID type, surfaced in ``context_builder``, ``collapse``,
``runner``) serves as the LangSmith trace ID — Pillar 8's "one namespace"
satisfied by reuse, not by generation.
"""

from orchestrator.observability.cost_dashboard import (
    detect_cost_anomalies,
    format_cost_breakdown_for_ops,
    get_tenant_cost,
    get_tenant_unit_economics,
    get_workspace_cost_summary,
    runaway_alert_candidates,
)
from orchestrator.observability.langsmith import (
    format_run_id_footer,
    get_project_name,
    is_enabled,
    trace_run,
    traceable_node,
    traceable_tool,
)
from orchestrator.observability.log import log_event, purge_pipeline_log_older_than
from orchestrator.observability.pii import redact_for_langsmith, redact_for_log
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
    "detect_cost_anomalies",
    "format_cost_breakdown_for_ops",
    "format_run_id_footer",
    "get_project_name",
    "get_tenant_cost",
    "get_tenant_unit_economics",
    "get_workspace_cost_summary",
    "is_enabled",
    "log_event",
    "purge_pipeline_log_older_than",
    "query_errors_recent",
    "query_event_type",
    "query_run",
    "query_tenant_recent",
    "redact_for_langsmith",
    "redact_for_log",
    "runaway_alert_candidates",
    "trace_run",
    "traceable_node",
    "traceable_tool",
]
