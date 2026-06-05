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
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# NEEDS-FAZAL: a Meta-approved template carrying the reply text as {{1}}. Until its SID is
# registered the send dry-runs/fails-safe — the handler's DB action still lands; only the
# owner reply is gated on the template.
_EDGE_ACK_TEMPLATE = "team_edge_case_ack"


def _send_edge_ack(tenant_id: UUID | str, recipient_phone: str | None, text: str) -> None:
    """Best-effort owner reply via the generic edge-case ack template. A send failure
    never undoes the handler's already-applied action (the reply is informational)."""
    from orchestrator.utils.twilio_send import send_template_message

    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    try:
        send_template_message(
            tid, _EDGE_ACK_TEMPLATE, {"1": text}, recipient_phone=recipient_phone or None
        )
    except Exception:
        logger.exception("edge-case ack send failed tenant=%s", tenant_id)


def route_edge_case(
    *, tenant_id: UUID | str, event: Any, classify_fn: Any | None = None
) -> Any | None:
    """Classify the owner message; route the PR-1 edge-cases (exclusion / status_query) to
    their fast handlers and return a DispatchResult. Return None to fall through to the
    agent (any other intent, incl. the PR-2 adhoc/template intents). ``classify_fn`` is
    injectable for tests (no live Anthropic)."""
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
        classification = getattr(classify_fn(body), "classification", None)
    except Exception:
        # A classify failure (bad model JSON / envelope validation) must NOT crash
        # dispatch or trigger a workflow retry — fall through to the agent (the prior
        # no-classify behaviour). The fast-path is an optimisation, never a hard gate.
        logger.warning("route_edge_case: classify failed; falling through to the agent")
        return None

    if classification == "exclusion_request":
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
        _send_edge_ack(tenant_id, sender_phone, text)
        return DispatchResult(
            final_status="completed", terminal_path="terminal", reason="edge_case:status_query"
        )

    # adhoc_campaign_request + template_error_followup -> PR-2 (VT-335). Everything else
    # falls through to the full agent (existing behaviour).
    return None
