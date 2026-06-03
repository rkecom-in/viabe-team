"""VT-47 — owner-approval resume path.

When an owner sends an inbound WhatsApp message while a run is PAUSED on an
owner-approval interrupt, that message is the approval decision. This module
owns the resume: classify the reply (VT-49), resolve the durable
pending_approvals row, and resume the LangGraph run via Command(resume=...).

Pillar 1: this is REASONING-adjacent (it calls the VT-49 classifier), so it
lives in the durable-workflow layer (called from runner.webhook_pipeline_run)
— NOT in the deterministic ingress endpoint (twilio_ingress.py stays pure
transport). The ingress endpoint must not classify (Pillar 1).

Pillar 7: the owner's decision is AUTHORITATIVE. The classifier maps:
  approval            -> approved      (the only verb that lets a send proceed)
  rejection           -> rejected
  question | feedback -> needs_changes (resumes; the agent decides next step)
  other / low-conf    -> NO resume (re-prompt is a Phase-2 loop; we do not
                         guess a decision — Pillar 7 forbids inventing approval)

CL-390: log approval_id + tenant_id + decision ONLY. Never the message body,
never the owner phone. owner_message_sid (a Twilio SID) is allowed.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# VT-49 Classification -> the durable pending_approvals.decision verb.
_CLASSIFICATION_TO_DECISION: dict[str, str] = {
    "approval": "approved",
    "rejection": "rejected",
    "question": "needs_changes",
    "feedback": "needs_changes",
    # 'other' deliberately absent -> no resume (do not guess; Pillar 7).
}

# pending_approvals.status the decision collapses to.
_DECISION_TO_STATUS: dict[str, str] = {
    "approved": "approved",
    "rejected": "rejected",
    "needs_changes": "rejected",  # needs_changes resolves the pause as non-approval
    "timeout": "timed_out",
}

# Below this confidence we do NOT resume — we leave the run paused and let the
# owner try again (Pillar 7: never auto-approve on a guess).
_MIN_CONFIDENCE = 0.5


def find_open_approval_for_tenant(
    conn: Any, tenant_id: UUID | str
) -> dict[str, Any] | None:
    """Return the most-recent UNRESOLVED approval for the tenant, else None.

    Tenant-scoped (RLS via the open tenant_connection). Used by the runner to
    decide whether an inbound message is an approval reply vs a normal message.
    """
    row = conn.execute(
        """
        SELECT id::text AS id, run_id::text AS run_id, approval_type,
               campaign_id::text AS campaign_id
        FROM pending_approvals
        WHERE tenant_id = %s AND resolved_at IS NULL
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        (str(tenant_id),),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return {
        "id": row[0], "run_id": row[1], "approval_type": row[2],
        "campaign_id": row[3],
    }


def resolve_decision_from_reply(
    text: str,
    *,
    tenant_id: UUID | str,
    classify_fn: Any | None = None,
) -> str | None:
    """Classify an owner reply (VT-49) and map it to a decision verb.

    Returns the decision ('approved'|'rejected'|'needs_changes') or None when
    the reply is not a clear decision (other / low-confidence) — None means
    "do not resume; leave paused" (Pillar 7: never guess approval).

    ``tenant_id`` (VT-270): threaded into classify so the transmit is owner_inputs-consent-gated;
    a no-consent skip returns classification='other' → None → leave paused (conservative).
    ``classify_fn`` defaults to VT-49 classify_owner_message; tests inject a stub so no live
    Anthropic call is made.
    """
    if classify_fn is None:
        from orchestrator.agent.tools.classify_owner_message import (
            ClassifyOwnerMessageInput,
            classify_owner_message,
        )

        def classify_fn(t: str) -> Any:  # noqa: ANN401
            return classify_owner_message(
                ClassifyOwnerMessageInput(text=t, tenant_id=str(tenant_id))
            )

    result = classify_fn(text)
    classification = getattr(result, "classification", None)
    confidence = float(getattr(result, "confidence", 0.0) or 0.0)

    decision = _CLASSIFICATION_TO_DECISION.get(str(classification))
    if decision is None:
        return None
    if confidence < _MIN_CONFIDENCE:
        # A low-confidence approval/rejection is not authoritative enough to
        # move a Pillar-7 gate. Leave paused.
        return None
    return decision


def mark_approval_resolved(
    conn: Any,
    approval_id: UUID | str,
    decision: str,
    *,
    owner_message_sid: str | None = None,
) -> None:
    """UPDATE the pending_approvals row with the resolved decision + status.

    Tenant-scoped (RLS via the open tenant_connection). Idempotent-ish: only
    updates rows still unresolved, so a redelivered reply does not re-resolve.
    """
    status = _DECISION_TO_STATUS.get(decision, "rejected")
    conn.execute(
        """
        UPDATE pending_approvals
        SET decision = %s, status = %s, resolved_at = now(),
            owner_message_sid = COALESCE(%s, owner_message_sid)
        WHERE id = %s AND resolved_at IS NULL
        """,
        (decision, status, owner_message_sid, str(approval_id)),
    )


def resume_run(run_id: UUID | str, decision: str) -> dict[str, Any]:
    """Resume the paused LangGraph run with the owner's decision.

    Re-enters the interrupting node (request_owner_approval_node) from its
    start; arm_pause_request is a no-op (the row is now resolved, not open)
    and interrupt() returns the resume payload. Returns the terminal state.

    Built with the SAME checkpointer + thread_id as the original invoke so the
    suspended checkpoint is found. The model is needed only to compile the
    graph; the resumed node does not call it.
    """
    from langgraph.types import Command

    from orchestrator.agent.dispatch import _resolve_model
    from orchestrator.graph import get_checkpointer
    from orchestrator.supervisor import build_supervisor_graph

    graph = build_supervisor_graph(
        model=_resolve_model(), checkpointer=get_checkpointer()
    )
    return graph.invoke(  # type: ignore[no-any-return]
        Command(resume={"decision": decision}),
        config={"configurable": {"thread_id": str(run_id)}},
    )


__all__ = [
    "find_open_approval_for_tenant",
    "mark_approval_resolved",
    "resolve_decision_from_reply",
    "resume_run",
]
