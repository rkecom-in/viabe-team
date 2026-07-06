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

from orchestrator.db.wrappers import PendingApprovalsWrapper

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

# VT-334 — an owner "defer" EXTENDS the window 48h; after this many defers it is treated as a
# rejection (decision='defer', status='rejected'). With max=2: the 1st defer extends, the 2nd
# is terminal (Cowork 20260606T103500Z (a)).
_MAX_DEFERS = 2


def find_open_approval_for_tenant(
    conn: Any, tenant_id: UUID | str
) -> dict[str, Any] | None:
    """Return the most-recent UNRESOLVED approval for the tenant, else None.

    Tenant-scoped (RLS via the open tenant_connection). Used by the runner to
    decide whether an inbound message is an approval reply vs a normal message.
    VT-306: reads through the typed wrapper on the caller's conn.
    """
    return PendingApprovalsWrapper().find_open_for_tenant(tenant_id, conn=conn)


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
    # VT-83: deterministic Pillar-7 fast-path for an UNAMBIGUOUS approve/reject — an LLM
    # must never decide a clear owner approval (a misread Hindi/Hinglish "no" would send a
    # campaign the owner REJECTED). A clear deterministic signal WINS; only genuinely
    # ambiguous text falls through to the Haiku classifier below.
    from orchestrator.owner_inputs.approval_reply import classify_approval_reply

    fast = classify_approval_reply(text)
    if fast is not None:
        return fast

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
    tenant_id: UUID | str,
    approval_id: UUID | str,
    decision: str,
    *,
    owner_message_sid: str | None = None,
    owner_feedback: str | None = None,
) -> bool:
    """Resolve (or, for a defer, EXTEND) the pending_approvals row. Returns True if the row was
    RESOLVED (the caller resumes the run), False if it was EXTENDED on a defer (the run STAYS
    paused, the window pushed out 48h).

    VT-306: tenant-predicated (the pre-migration ``WHERE id`` UPDATE was an IDOR gap). Idempotent-
    ish: only resolves rows still unresolved.

    VT-334 defer: a 'defer' decision extends the window (defer_count++, timeout +48h, still
    pending) until ``_MAX_DEFERS``, after which it resolves as decision='defer', status='rejected'
    (the SAFE downstream behavior; the audit truth is decision='defer').

    VT-369: this is the single resolution choke point (owner-reply path AND the 30-min timeout
    sweep), so the agent-surface glue lives HERE: when the resolved row is an
    ``agent_customer_send`` approval, ``approval_glue.apply_agent_decision`` flips the linked
    ``agent_draft_batches`` row in the SAME transaction/connection (approved → 'approved';
    needs_changes → 'edit_requested' + owner_feedback + ONE-regeneration cap; rejected →
    'rejected'; timeout / exhausted-defer → 'cancelled'). ``owner_feedback`` is the raw owner
    reply body — persisted on the RLS-protected batch row only, NEVER logged (CL-390)."""
    from orchestrator.observability.tm_audit import emit_tm_audit
    if decision == "defer":
        new_count = PendingApprovalsWrapper().extend_on_defer(
            tenant_id, approval_id, timeout_hours=48, conn=conn
        )
        if new_count < _MAX_DEFERS:
            logger.info(
                "approval defer extended approval_id=%s tenant=%s defer_count=%s",
                approval_id, tenant_id, new_count,
            )
            return False  # still pending — do NOT resume
        # Exhausted: resolve as a rejection (status), keep decision='defer' for the audit.
        PendingApprovalsWrapper().mark_resolved(
            tenant_id, approval_id,
            decision="defer", status="rejected",
            owner_message_sid=owner_message_sid, conn=conn,
        )
        emit_tm_audit(
            event_layer="does",
            event_kind="approval_resolved",
            actor="team_manager",
            tenant_id=tenant_id,
            run_id=None,
            action={
                "approval_id": str(approval_id),
                "decision": "defer",
                "status": "rejected",
                "owner_message_sid": owner_message_sid,
            },
            summary=f"approval resolved: decision={decision}",
            conn=conn,
        )
        _apply_agent_glue(conn, tenant_id, approval_id, "defer", owner_feedback)
        return True

    status = _DECISION_TO_STATUS.get(decision, "rejected")
    PendingApprovalsWrapper().mark_resolved(
        tenant_id,
        approval_id,
        decision=decision,
        status=status,
        owner_message_sid=owner_message_sid,
        conn=conn,
    )
    emit_tm_audit(
        event_layer="does",
        event_kind="approval_resolved",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=None,
        action={
            "approval_id": str(approval_id),
            "decision": decision,
            "status": status,
            "owner_message_sid": owner_message_sid,
        },
        summary=f"approval resolved: decision={decision}",
        conn=conn,
    )
    _apply_agent_glue(conn, tenant_id, approval_id, decision, owner_feedback)
    return True


def _apply_agent_glue(
    conn: Any,
    tenant_id: UUID | str,
    approval_id: UUID | str,
    decision: str,
    owner_feedback: str | None,
) -> None:
    """VT-369 — apply the agent-batch consequence of a TRUE resolution (no-op for
    non-agent approval types; ``apply_agent_decision`` reads the row's
    ``approval_type``/``draft_batch_id`` on the caller's conn and returns None
    unless it is an open-batch ``agent_customer_send``). Same txn as the resolve.

    VT-609 fix round 2 (CRITICAL): dispatch is BY ``approval_type``, additively — each glue call
    below self-guards on the row's own ``approval_type`` and no-ops for any other row, so adding a
    new type's glue here can never change another type's behavior. ``apply_business_policy_decision``
    is the ``business_policy_grant`` leg: it is what actually calls ``grant_business_policy`` on the
    owner's clear yes (a specialist tool trying to do this from its own turn was never reliably
    re-dispatched — see that function's docstring). Both calls run unconditionally; at most one is
    ever non-no-op for a given row, since a row has exactly one ``approval_type``."""
    from orchestrator.agents.approval_glue import apply_agent_decision
    from orchestrator.agents.business_policy import apply_business_policy_decision

    apply_agent_decision(
        conn,
        tenant_id,
        {"id": str(approval_id)},
        decision,
        owner_feedback=owner_feedback,
    )
    apply_business_policy_decision(conn, tenant_id, approval_id, decision)


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
