"""Observability layer (VT-101).

LangSmith integration + PII redaction. The ``run_id`` already plumbed through
the orchestrator (UUID type, surfaced in ``context_builder``, ``collapse``,
``runner``) serves as the LangSmith trace ID — Pillar 8's "one namespace"
satisfied by reuse, not by generation.
"""

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
from orchestrator.observability.types import PipelineLogEvent

__all__ = [
    "PipelineLogEvent",
    "format_run_id_footer",
    "get_project_name",
    "is_enabled",
    "log_event",
    "purge_pipeline_log_older_than",
    "query_errors_recent",
    "query_event_type",
    "query_run",
    "query_tenant_recent",
    "redact_for_langsmith",
    "redact_for_log",
    "trace_run",
    "traceable_node",
    "traceable_tool",
]
