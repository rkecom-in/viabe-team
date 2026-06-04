"""VT-66 — L2 episodic memory types + templated summaries.

Centralized event-type vocabulary (durable, lowercase snake — same posture as
kg_vocab) + the Pydantic envelope + per-type summary templates. Summaries are
Python-templated, NEVER LLM-generated (Pillar 1; the agent CONSUMES L2, it does
not author it). Payloads are structured + carry NO raw PII (CL-390).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class L2EventType:
    CAMPAIGN_PROPOSED: Final = "campaign_proposed"
    CAMPAIGN_APPROVED: Final = "campaign_approved"
    CAMPAIGN_REJECTED: Final = "campaign_rejected"
    CAMPAIGN_SENT: Final = "campaign_sent"
    ATTRIBUTION_CLOSED: Final = "attribution_closed"
    # VT-312 (brain-decides): NO fixed-threshold DETECTOR emits these — the agent
    # judges dormant / high-value contextually from the tenant's own distributions
    # (context_builder._build_ledger_summary). REPURPOSED to agent-action customer
    # markers: the brain emits a customer-referencing episodic row when it ACTS on
    # a customer (the emit-site = VT-320), which is also what keeps VT-76's
    # reconstitution sweep non-degenerate. Kept in the schema (no CHECK churn).
    CUSTOMER_DORMANT_THRESHOLD_CROSSED: Final = "customer_dormant_threshold_crossed"
    CUSTOMER_HIGH_VALUE_THRESHOLD_CROSSED: Final = "customer_high_value_threshold_crossed"
    OWNER_MESSAGE_RECEIVED: Final = "owner_message_received"
    AGENT_DISPATCH_COMPLETED: Final = "agent_dispatch_completed"
    AGENT_DISPATCH_TERMINATED: Final = "agent_dispatch_terminated"
    PHASE_TRANSITIONED: Final = "phase_transitioned"
    CLARIFICATION_RESOLVED: Final = "clarification_resolved"
    # VT-320: emitted when the agent ACTS on a specific customer (a campaign
    # send) — a customer-referencing episodic row so VT-76 reconstitution has
    # real rows to anonymize on opt-out. Distinct from the repurposed
    # *_threshold_crossed names (semantics matter on this privacy taxonomy).
    CUSTOMER_ACTION_TAKEN: Final = "customer_action_taken"


L2_EVENT_TYPES: Final = (
    L2EventType.CAMPAIGN_PROPOSED, L2EventType.CAMPAIGN_APPROVED,
    L2EventType.CAMPAIGN_REJECTED, L2EventType.CAMPAIGN_SENT,
    L2EventType.ATTRIBUTION_CLOSED, L2EventType.CUSTOMER_DORMANT_THRESHOLD_CROSSED,
    L2EventType.CUSTOMER_HIGH_VALUE_THRESHOLD_CROSSED, L2EventType.OWNER_MESSAGE_RECEIVED,
    L2EventType.AGENT_DISPATCH_COMPLETED, L2EventType.AGENT_DISPATCH_TERMINATED,
    L2EventType.PHASE_TRANSITIONED, L2EventType.CLARIFICATION_RESOLVED,
    L2EventType.CUSTOMER_ACTION_TAKEN,
)

# Templated summaries — NO LLM, NO raw PII (use ids/counts/amounts, never names/phones).
# Missing keys fall back to the generic template (defensive — never raises).
_SUMMARY_TEMPLATES: Final[dict[str, str]] = {
    L2EventType.CAMPAIGN_PROPOSED: "Campaign {campaign_id} proposed.",
    L2EventType.CAMPAIGN_APPROVED: "Campaign {campaign_id} approved by owner.",
    L2EventType.CAMPAIGN_REJECTED: "Campaign {campaign_id} rejected by owner.",
    L2EventType.CAMPAIGN_SENT: "Campaign {campaign_id} sent to {recipient_count} customers.",
    L2EventType.ATTRIBUTION_CLOSED: "Campaign {campaign_id} attribution closed: {arrr_paise} paise recovered.",
    L2EventType.CUSTOMER_DORMANT_THRESHOLD_CROSSED: "Customer crossed dormancy threshold (cohort: {cohort}, {days_dormant}d).",
    L2EventType.CUSTOMER_HIGH_VALUE_THRESHOLD_CROSSED: "Customer crossed high-value threshold ({lifetime_paise} paise lifetime).",
    L2EventType.AGENT_DISPATCH_COMPLETED: "Agent dispatch completed ({outcome}).",
    L2EventType.AGENT_DISPATCH_TERMINATED: "Agent dispatch terminated ({reason}).",
    L2EventType.OWNER_MESSAGE_RECEIVED: "Owner message received ({message_type}, {body_length} chars).",
    L2EventType.PHASE_TRANSITIONED: "Phase {from_phase} -> {to_phase}.",
    L2EventType.CLARIFICATION_RESOLVED: "Clarification resolved: {decision}.",
    L2EventType.CUSTOMER_ACTION_TAKEN: "Agent acted on a customer ({action}).",
}


def render_summary(event_type: str, payload: dict[str, Any]) -> str:
    """Render a templated, PII-free summary. Never raises (defensive format)."""
    template = _SUMMARY_TEMPLATES.get(event_type)
    if template is None:
        return f"{event_type} occurred."
    try:
        return template.format_map(_SafeDict(payload))
    except Exception:  # noqa: BLE001 — a summary must never break the writer
        return f"{event_type} occurred."


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "?"


class EpisodicEvent(BaseModel):
    """One L2 episodic event (matches the episodic_events row)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    event_type: str
    summary: str | None
    payload: dict[str, Any]
    referenced_entity_type: str | None
    referenced_entity_id: UUID | None
    occurred_at: datetime


__all__ = ["EpisodicEvent", "L2_EVENT_TYPES", "L2EventType", "render_summary"]
