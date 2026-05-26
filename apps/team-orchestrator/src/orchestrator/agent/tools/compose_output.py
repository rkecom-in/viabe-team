"""Composer tool wrapper for the orchestrator-agent (VT-30).

Adapter that exposes :func:`orchestrator.output_composer.compose_owner_output`
to the orchestrator-agent's tool inventory. Pattern parallels
``self_evaluate.py`` (langchain ``@tool`` decoration) — the agent
dispatches the tool via ``ChatAnthropic``'s tool-call mechanism.

VT-181 retrofit: ``@tool_step`` wraps the function for observability —
each invocation writes a pipeline_steps row via VT-180's write_step.
ContextVar ``_observability_context`` must be set by the caller (per
CL-Q1 Option A); without it the decorator logs + skips the write.

Forward-pointing
----------------
This module imports ``langchain_core.tools.tool`` to register the
wrapper. The composer module itself (``output_composer.py``) does NOT
import langchain — it stays in the scan scope of
``gate-no-llm-in-deterministic-triggers`` (extended in this PR to scan
``output_composer.py`` whole-file). Splitting the tool wrapper out
preserves the gate's invariant.

VT-125 (orchestrator-agent prompt + tool inventory expansion, Backlog
exec 9) will wire this tool into the inventory. The wrapper is shipped
here so VT-125 can pick it up without re-registering the function.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict

from orchestrator.observability.decorators import tool_step
from orchestrator.output_composer import (
    ComposedOutput,
    compose_owner_output,
)


class _ComposeOwnerOutputInput(BaseModel):
    """VT-181 envelope shape for compose_owner_output_tool args."""

    model_config = ConfigDict(extra="forbid")

    intent_or_trigger: str
    tenant_id: str
    phase: str
    last_owner_message_at_iso: str | None = None
    escalation_pending: bool = False
    specialist_result_json: dict[str, Any] | None = None


class _ComposeOwnerOutputOutput(BaseModel):
    """VT-181 envelope shape for compose_owner_output_tool return dict."""

    model_config = ConfigDict(extra="allow")

    message_body: str
    message_type: str
    template_name: str | None = None
    template_params: dict[str, Any] | None = None
    urgency: str | None = None
    follow_up_required: bool | None = None
    follow_up_intent: str | None = None
    preferred_language: str | None = None
    signature: str | None = None
    honesty_notes: list[str] | None = None


@tool
@tool_step(
    step_kind="mcp_tool_call",
    envelope_in=_ComposeOwnerOutputInput,
    envelope_out=_ComposeOwnerOutputOutput,
    step_name="compose_owner_output",
)
def compose_owner_output_tool(
    intent_or_trigger: str,
    tenant_id: str,
    phase: str,
    last_owner_message_at_iso: str | None = None,
    escalation_pending: bool = False,
    specialist_result_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose an owner-facing WhatsApp message from a specialist result + current state.

    Args:
        intent_or_trigger: Routing key (e.g. ``"welcome"``,
            ``"weekly_approval"``, ``"agent_stuck"``).
        tenant_id: UUID string of the tenant the message is addressed to.
        phase: Current subscriber phase (``"onboarding"`` / ``"trial"`` /
            ``"paid_active"`` / ``"paid_at_risk"`` / ``"refunded"`` etc.).
        last_owner_message_at_iso: ISO-8601 UTC timestamp of the last
            owner-facing message, or ``None`` if no prior message. Drives
            the 24-hour-window template-vs-free-form decision.
        escalation_pending: True iff an escalation is pending for this
            tenant; prepends honest framing.
        specialist_result_json: Optional specialist agent's structured
            output. Schema: ``{"status": str, "terminated_by": str | None,
            "output": dict, ...}``.

    Returns:
        Dict serialisation of :class:`ComposedOutput`. The orchestrator-
        agent passes the returned ``signature`` to downstream
        ``send_template_message`` (template path) or to the future
        ``send_whatsapp_message`` wrapper (free-form path; VT-125
        verifies the signature server-side).

    Tool guidance: ALWAYS call this BEFORE invoking ``send_whatsapp_message``
    or ``send_whatsapp_template``. Direct-handler-path messages (DSR ack,
    opt-out confirm, etc.) bypass this tool by design.
    """
    from datetime import datetime
    from types import SimpleNamespace

    state = {
        "tenant_id": UUID(tenant_id),
        "phase": phase,
        "escalation_pending": escalation_pending,
        "last_owner_message_at": (
            datetime.fromisoformat(last_owner_message_at_iso)
            if last_owner_message_at_iso
            else None
        ),
    }

    specialist_result: Any | None = None
    if specialist_result_json is not None:
        specialist_result = SimpleNamespace(**specialist_result_json)
        # Ensure ``output`` exists (some callers pass it as dict; others as None).
        if not hasattr(specialist_result, "output"):
            specialist_result.output = {}

    out: ComposedOutput = compose_owner_output(
        specialist_result, state, intent_or_trigger
    )
    return {
        "message_body": out.message_body,
        "message_type": out.message_type,
        "template_name": out.template_name,
        "template_params": out.template_params,
        "urgency": out.urgency,
        "follow_up_required": out.follow_up_required,
        "follow_up_intent": out.follow_up_intent,
        "preferred_language": out.preferred_language,
        "signature": out.signature,
        "honesty_notes": out.honesty_notes,
    }


__all__ = ["compose_owner_output_tool"]
