"""VT-84 — stage-2 edge-case router.

After Haiku classification (in the brain dispatch), intercept the deterministic
edge-case intents and route them to FAST-PATH handlers, skipping the full
orchestrator-agent reasoning loop. Returns a ``DispatchResult`` to terminate the run,
or ``None`` to fall through to the agent.

Pillar 7: Haiku ROUTES the intent (low stakes — a misroute lands on the agent or a
no-op); the ACTION is deterministic — exclusion is a reversible flag + deterministic
customer resolution (never auto-pick on ambiguity); status_query is read-only SQL. A
SEND (adhoc campaign) is deliberately NOT routed here — it stays behind the approval gate
(VT-335 / PR-2): a customer-facing send must never fire off a Haiku intent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from orchestrator.agent.dispatch import DispatchResult

logger = logging.getLogger(__name__)

# VT-336: minimum Haiku confidence to route a mutating exclusion_request to the fast handler.
# Below this, fall through to the agent (the misroute lands on reasoning, not an auto-exclude).
_EXCLUSION_CONFIDENCE_FLOOR = 0.7

def _send_edge_ack(tenant_id: UUID | str, recipient_phone: str | None, text: str) -> None:
    """VT-349: the edge-case ack is a DIRECT in-window reply to the owner's just-sent message →
    a FREE-FORM session message (not a template). The handler already computed `text`
    (locale-aware), so we send it as-is. Best-effort + fail-safe — a window-closed/failed send
    never undoes the handler's already-applied action (the reply is informational)."""
    from orchestrator.owner_surface.freeform_acks import send_freeform_ack

    send_freeform_ack(tenant_id, recipient_phone, text)


def route_edge_case(
    *,
    tenant_id: UUID | str,
    event: Any,
    classify_fn: Any | None = None,
    intent_sink: dict[str, Any] | None = None,
) -> "DispatchResult | Literal['owner_initiated'] | None":
    """Classify the owner message; route the PR-1 edge-cases (exclusion / status_query) to
    their fast handlers and return a DispatchResult. Return None to fall through to the
    agent (any other intent, incl. the PR-2 adhoc/template intents). ``classify_fn`` is
    injectable for tests (no live Anthropic).

    VT-461: the SAME classification this router already runs is surfaced to the
    Team-Manager brain when the turn falls through to the agent. Pass ``intent_sink``
    (a mutable dict); on a successful classify it is populated with the typed envelope
    (``classification`` / ``confidence`` / ``suggested_action``) so dispatch can inject a
    ``## Manager intent signal`` prior WITHOUT a second Haiku call. A classify failure
    leaves the sink untouched (the brain reasons from the message alone)."""
    from orchestrator.agent.dispatch import DispatchResult  # lazy: avoid import cycle

    body = getattr(event, "body", "") or ""
    sender_phone = getattr(event, "sender_phone", None)

    if classify_fn is None:
        from orchestrator.agent.tools.classify_owner_message import (
            ClassifyOwnerMessageInput,
            classify_owner_message,
        )

        def classify_fn(text: str) -> Any:  # noqa: ANN401
            return classify_owner_message(
                ClassifyOwnerMessageInput(text=text, tenant_id=str(tenant_id))
            )

    try:
        _out = classify_fn(body)
        classification = getattr(_out, "classification", None)
        confidence = float(getattr(_out, "confidence", 0.0) or 0.0)
        # VT-461: surface the classification to the brain (handle-directly-vs-delegate
        # prior). Only the typed fields — never the raw body — cross into the sink.
        if intent_sink is not None and classification is not None:
            intent_sink["classification"] = classification
            intent_sink["confidence"] = confidence
            intent_sink["suggested_action"] = str(
                getattr(_out, "suggested_action", "") or ""
            )
    except Exception:
        # A classify failure (bad model JSON / envelope validation) must NOT crash
        # dispatch or trigger a workflow retry — fall through to the agent (the prior
        # no-classify behaviour). The fast-path is an optimisation, never a hard gate.
        logger.warning("route_edge_case: classify failed; falling through to the agent")
        return None

    if classification == "exclusion_request":
        # VT-336: exclusion MUTATES state (reversible + owner-acked, but still). Require a
        # confidence floor — a low-confidence misroute falls through to the agent rather than
        # auto-excluding. (status_query below is read-only, so it needs no floor.)
        if confidence < _EXCLUSION_CONFIDENCE_FLOOR:
            logger.info(
                "route_edge_case: exclusion_request below confidence floor (%.2f < %.2f) — "
                "fall through to the agent",
                confidence, _EXCLUSION_CONFIDENCE_FLOOR,
            )
            return None
        from orchestrator.owner_inputs.exclusion import handle_exclusion

        result = handle_exclusion(tenant_id, body)
        _send_edge_ack(tenant_id, sender_phone, result.response_text)
        return DispatchResult(
            final_status="completed",
            terminal_path="terminal",
            reason=f"edge_case:exclusion:{result.action}",
        )

    if classification == "status_query":
        from orchestrator.owner_inputs.status_query import answer_status_query

        text = answer_status_query(tenant_id, body)
        if text is None:
            # VT-600 — the classifier said status_query but the deterministic parse
            # found no lookup it owns (e.g. "did you get my store address?"): fall
            # through to the brain instead of the old portal deflection. The
            # classification still rides intent_sink as the brain's prior.
            logger.info(
                "route_edge_case: status_query parse=unknown — falling through to the agent"
            )
            return None
        _send_edge_ack(tenant_id, sender_phone, text)
        return DispatchResult(
            final_status="completed", terminal_path="terminal", reason="edge_case:status_query"
        )

    if classification == "template_error_followup":
        from orchestrator.owner_inputs.template_error import handle_template_error

        te_result = handle_template_error(tenant_id, body)
        _send_edge_ack(tenant_id, sender_phone, te_result.response_text)
        return DispatchResult(
            final_status="completed",
            terminal_path="terminal",
            reason="edge_case:template_error",
        )

    if classification == "adhoc_campaign_request":
        # Cowork Q4 hard invariant: a SEND must NEVER fire off a Haiku intent. We do NOT
        # fast-path a send here — we return the "owner_initiated" trigger marker and FALL
        # THROUGH to the agent, which builds a CampaignPlan that the standard
        # request_owner_approval gate confirms before any execute. route_after_approval
        # keys on owner_decision (not trigger_reason), so owner_initiated CANNOT bypass it.
        return "owner_initiated"

    # Everything else (approval / rejection / question / feedback / first_data_step /
    # other) falls through to the full agent (existing behaviour).
    return None
