"""Agent reasoning trace capture (VT-104).

Forward-pointing module: these functions are callable but no production
code path currently invokes them. The VT-4 agent SDK integration that
wires the call sites is a separate VT row (TBD). DO NOT modify the
function signatures without updating the future-PR brief, since those
signatures are the contract that agent-SDK PR will integrate against.

PII redaction policy
--------------------
Every payload that lands in ``pipeline_log`` flows through the canonical
:func:`orchestrator.privacy.pii_redactor.redact` at the
:func:`orchestrator.observability.log.log_event` boundary. The capture
functions here pass payloads through unchanged; the writer handles the
redaction. This keeps redaction at a single seam — Pillar 8 (one
redactor).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from orchestrator.observability.log import log_event


def capture_agent_reasoning_step(
    run_id: UUID | str,
    tenant_id: UUID | str | None,
    *,
    step_name: str,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
    component: str = "agent",
) -> None:
    """Emit an ``agent_reasoning_step`` event for the agent's planning loop.

    ``content`` is the human-readable reasoning text; redacted at the
    writer boundary.
    """
    payload: dict[str, Any] = {"step_name": step_name}
    if content is not None:
        payload["content"] = content
    if metadata:
        payload["metadata"] = metadata
    log_event(
        event_type="agent_reasoning_step",
        run_id=run_id,
        tenant_id=tenant_id,
        severity="info",
        component=component,
        payload=payload,
    )


def capture_tool_call_args(
    run_id: UUID | str,
    tenant_id: UUID | str | None,
    *,
    tool_name: str,
    args: dict[str, Any] | None = None,
    component: str = "agent",
) -> None:
    """Emit a ``tool_call_args`` event for an outgoing tool invocation."""
    payload: dict[str, Any] = {"tool_name": tool_name}
    if args is not None:
        payload["args"] = args
    log_event(
        event_type="tool_call_args",
        run_id=run_id,
        tenant_id=tenant_id,
        severity="info",
        component=component,
        payload=payload,
    )


def capture_tool_call_result(
    run_id: UUID | str,
    tenant_id: UUID | str | None,
    *,
    tool_name: str,
    ok: bool,
    result: Any = None,
    error: str | None = None,
    component: str = "agent",
) -> None:
    """Emit a ``tool_call_result`` event for the tool's response."""
    payload: dict[str, Any] = {"tool_name": tool_name, "ok": ok}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    severity = "info" if ok else "warn"
    log_event(
        event_type="tool_call_result",
        run_id=run_id,
        tenant_id=tenant_id,
        severity=severity,
        component=component,
        payload=payload,
    )


__all__ = [
    "capture_agent_reasoning_step",
    "capture_tool_call_args",
    "capture_tool_call_result",
]
