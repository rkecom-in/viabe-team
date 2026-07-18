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
  feedback            -> needs_changes (an explicit change request; resume + re-draft)
  question            -> NO resume (VT-632: a QUESTION IS NOT A DECISION — answer it and leave the
                         approval PENDING; do NOT resolve/reject/re-draft the campaign just because
                         the owner asked something. This aligns the Haiku layer with the module's
                         OWN deterministic fast-path, which already treats any '?' as None. The bug
                         this closes: an UNRELATED owner question during a pending approval (e.g. a
                         product FAQ, a topic switch) was classified 'question' -> needs_changes,
                         which REJECTED + re-armed + re-sent the same approval ask verbatim instead
                         of answering — the dominant conversational trust-breaker.)
  other / low-conf    -> NO resume (re-prompt is a Phase-2 loop; we do not
                         guess a decision — Pillar 7 forbids inventing approval)

CL-390: log approval_id + tenant_id + decision ONLY. Never the message body,
never the owner phone. owner_message_sid (a Twilio SID) is allowed.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any
from uuid import UUID

from orchestrator.db.wrappers import PendingApprovalsWrapper

logger = logging.getLogger(__name__)

# VT-49 Classification -> the durable pending_approvals.decision verb.
_CLASSIFICATION_TO_DECISION: dict[str, str] = {
    "approval": "approved",
    "rejection": "rejected",
    "feedback": "needs_changes",  # an EXPLICIT change request -> resume + re-draft
    # VT-632: 'question' deliberately absent -> None -> NO resume. A question is not a decision;
    # it falls through to normal dispatch (the brain ANSWERS it) and the approval stays PENDING.
    # Mapping it to needs_changes let an unrelated FAQ / topic-switch REJECT + re-arm + re-send the
    # approval ask verbatim (the conversational trust-breaker). Consistent with classify_approval_
    # reply's own 'any ? -> None' rule (owner_inputs/approval_reply.py).
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

# Customer-facing SEND approval types (money): a real customer send. For these, an ambiguous reply
# (the deterministic classifier returned None) must NEVER be resolved to 'approved' by the LLM —
# a send proceeds ONLY on an UNAMBIGUOUS deterministic explicit approval (Pillar 7, official §2
# 2026-07-10 m_conversation_interruption breaker). Non-send approvals (autonomy_upgrade,
# business_policy_grant, …) may still use the Haiku fallback for genuinely ambiguous text.
_CUSTOMER_SEND_APPROVAL_TYPES = frozenset({"campaign_send", "agent_customer_send"})

# VT-334 — an owner "defer" EXTENDS the window 48h; after this many defers it is treated as a
# rejection (decision='defer', status='rejected'). With max=2: the 1st defer extends, the 2nd
# is terminal (Cowork 20260606T103500Z (a)).
_MAX_DEFERS = 2

# CD5 (§7D audit; Fazal ruling 2026-07-12) — an EXPLICIT owner "skip the review, just send it" waiver
# on a customer SEND is HONORED (the deterministic approval stands; the landed >12-token ambiguity
# gate in classify_approval_reply remains the safety floor), but waiving the human-review step on a
# real customer send is a governance-relevant act that MUST leave an audit trail. These markers
# detect that explicit waiver — they are NOT a decision input (never change the return value), only
# the trigger for the audit record. Tight but GENERAL: the review-waiving phrases an owner actually
# types (EN + Hinglish), matched as normalized substrings — not a match on any one scenario string.
_SKIP_REVIEW_MARKERS = (
    "skip review",
    "skip the review",
    "bina review",
    "without review",
    "no review",
    "review mat",          # "don't review it"
    "kya review",          # rhetorical "what's to review" (sr_always_confirm_first_contact_floor)
    "review ki zaroorat",  # "no need to review"
    "seedha bhej",         # "just send it straight" (sr_always_confirm_first_contact_floor)
    "seedhe bhej",
    "direct bhej",
    "directly bhej",
)


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
    approval_type: str | None = None,
    classify_fn: Any | None = None,
) -> str | None:
    """Resolve an owner approval reply to a decision verb.

    VT-648 (the MONEY GATE) — the ``TEAM_SEND_INTENT_LLM`` flag gates an LLM-primary send-intent
    classifier ON TOP of the deterministic path, for customer-SEND approvals ONLY. Three states:
      - ``off`` (default) — pure deterministic path, byte-for-byte the pre-VT-648 behavior.
      - ``shadow``        — deterministic still DECIDES; the LLM runs and its decision is LOGGED
                            alongside for comparison (no behavior change, no second effect).
      - ``enforce``       — for a customer-SEND the LLM + hard-stop veto DECIDE the gate.
    The flag is read ONCE here (a mode flip must not change behavior mid-turn). For a NON-customer-
    send approval type the flag has no effect — that path is unchanged in every mode. The fail-safe
    is identical to the deterministic path: uncertain / veto / low-confidence / LLM error → None.
    """
    from orchestrator.owner_inputs.send_intent import (
        decide_send_intent_enforce,
        get_send_intent_mode,
        shadow_log_send_intent,
    )

    is_customer_send = approval_type in _CUSTOMER_SEND_APPROVAL_TYPES
    mode = get_send_intent_mode()

    # ENFORCE: for a customer-SEND (money) the LLM + hard-stop veto own the gate. Structurally
    # money-safe — decide_send_intent_enforce returns 'approved' ONLY on a grounded, confident,
    # un-vetoed LLM approve; every other path (veto/hold/low-conf/ungrounded/error) is a non-approve.
    if mode == "enforce" and is_customer_send:
        return decide_send_intent_enforce(text, tenant_id=str(tenant_id))

    # OFF (default) + SHADOW: the deterministic path DECIDES (unchanged).
    decision = _resolve_decision_deterministic(
        text, tenant_id=tenant_id, approval_type=approval_type, classify_fn=classify_fn
    )

    # SHADOW: log what enforce WOULD have decided, alongside the live deterministic decision. Fail-
    # soft + PII-safe (no reply body / cue logged); never affects ``decision``.
    if mode == "shadow" and is_customer_send:
        shadow_log_send_intent(text, tenant_id=str(tenant_id), deterministic_decision=decision)
    return decision


def _resolve_decision_deterministic(
    text: str,
    *,
    tenant_id: UUID | str,
    approval_type: str | None = None,
    classify_fn: Any | None = None,
) -> str | None:
    """Classify an owner reply (VT-49) and map it to a decision verb.

    Returns the decision ('approved'|'rejected'|'needs_changes') or None when
    the reply is not a clear decision (other / low-confidence) — None means
    "do not resume; leave paused" (Pillar 7: never guess approval).

    ``tenant_id`` (VT-270): threaded into classify so the transmit is owner_inputs-consent-gated;
    a no-consent skip returns classification='other' → None → leave paused (conservative).
    ``approval_type``: the pending row's type. For a customer-facing SEND (money;
    ``_CUSTOMER_SEND_APPROVAL_TYPES``) an ambiguous reply (deterministic classifier None) is NOT
    escalated to the LLM — a send needs an UNAMBIGUOUS explicit approval, so None means "re-ask,
    leave paused", never a Haiku-guessed 'approved'.
    ``classify_fn`` defaults to VT-49 classify_owner_message; tests inject a stub so no live
    Anthropic call is made.
    """
    # VT-83: deterministic Pillar-7 fast-path for an UNAMBIGUOUS approve/reject — an LLM
    # must never decide a clear owner approval (a misread Hindi/Hinglish "no" would send a
    # campaign the owner REJECTED). A clear deterministic signal WINS; only genuinely
    # ambiguous text falls through to the Haiku classifier below.
    from orchestrator.owner_inputs.approval_reply import (
        classify_approval_reply,
        is_weak_ack_only_approval,
    )

    fast = classify_approval_reply(text)
    if fast is not None:
        # Money middle-path (Fazal 2026-07-12): a bare WEAK ack ("theek hai"/"ok", no send verb, no
        # strong yes) is NOT an unambiguous approval of a specific customer SEND — HOLD (None ->
        # re-ask), never auto-send on the ambiguous ack. Unambiguous approvals ("theek hai bhej do",
        # "haan bhej do") carry an explicit send verb / strong yes -> not weak-ack-only -> still
        # approve. Scoped to customer-SEND approvals; non-send approvals (autonomy_upgrade, etc.)
        # keep their existing bare-ack behavior.
        if (
            fast == "approved"
            and approval_type in _CUSTOMER_SEND_APPROVAL_TYPES
            and is_weak_ack_only_approval(text)
        ):
            return None
        # CD5 (§7D audit; Fazal ruling 2026-07-12): an EXPLICIT owner "skip review, seedha bhej do" on
        # a customer SEND is HONORED (the deterministic approval above STANDS) but must be AUDITED —
        # waiving the human-review step on a real customer send is governance-relevant. AUDIT-ONLY:
        # this NEVER changes the return value; fail-soft so an audit error can't affect the send
        # (Pillar 7 — the owner's authorized send is not held on an observability write).
        if (
            fast == "approved"
            and approval_type in _CUSTOMER_SEND_APPROVAL_TYPES
            and _has_skip_review_marker(text)
        ):
            _audit_owner_skip_review(tenant_id, approval_type)
        return fast

    # Money-safety: a customer-SEND approval never rides the LLM for an ambiguous reply. The
    # deterministic classifier was ambiguous (None) — for a send, that means re-ask (leave paused),
    # NEVER a Haiku-guessed approval. (A reject too: an ambiguous reply leaving the send paused is
    # the fail-safe direction — no unconsented send.)
    if approval_type in _CUSTOMER_SEND_APPROVAL_TYPES:
        return None

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


def _has_skip_review_marker(text: str) -> bool:
    """CD5 (§7D) — True iff the reply carries an EXPLICIT owner "skip the review, just send" waiver.

    Detector for the audit record ONLY — never a decision input. Normalization (NFC + casefold +
    apostrophe-strip) is kept in lockstep with ``classify_approval_reply`` so the markers match the
    exact text the classifier read. GENERAL: a small set of review-waiving phrases (EN + Hinglish)
    matched as substrings, not a match on the specific canary scenario string.
    """
    normalized = (
        unicodedata.normalize("NFC", (text or "").casefold())
        .replace("'", "")
        .replace("’", "")
    )
    return any(marker in normalized for marker in _SKIP_REVIEW_MARKERS)


def _audit_owner_skip_review(tenant_id: UUID | str, approval_type: str | None) -> None:
    """CD5 (§7D) — emit the audit for an owner who explicitly WAIVED review on a customer SEND.

    AUDIT-ONLY + FAIL-SOFT: the approval decision is unchanged and an audit failure must NEVER affect
    it (Pillar 7 — the owner's authorized send is not held on an observability write). ``conn`` is
    None (best-effort service-role write; ``emit_tm_audit``'s conn=None path already never raises),
    and the whole call is additionally wrapped so an import/attr error can't escape either. PII-safe
    (CL-390): the owner reply body is NEVER passed — only ``approval_type`` + structured facts.
    """
    try:
        from orchestrator.observability.tm_audit import emit_tm_audit

        emit_tm_audit(
            event_layer="decides",
            event_kind="owner_skip_review_authorized",
            actor="team_manager",
            tenant_id=tenant_id,
            summary="owner explicitly waived human review on a customer send — decision HONORED, "
            "audited (§7D); the >12-token ambiguity gate remains the safety floor",
            decision={
                "approval_type": approval_type,
                "decision": "approved",
                "review_waived": True,
            },
        )
    except Exception:  # noqa: BLE001 — audit is never a gate on the owner's authorized send
        logger.warning(
            "CD5 skip-review audit emit failed (fail-soft) tenant=%s", tenant_id, exc_info=True
        )


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
    resolved_rows = PendingApprovalsWrapper().mark_resolved(
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
    # VT-668 — money-path anti-silence: an approved campaign_send whose loop executor is dead must
    # never resolve into silence. Runs on this resolve conn (redrive commits with the resolution);
    # fail-soft, and a no-op for every non-approved / non-campaign_send / live-loop resolution. Gated
    # on the resolve having ACTUALLY applied (rowcount>0) so a double-resolve re-entry is a clean
    # no-op — no second redrive/ack (the FIRST resolution already handled the consumer).
    if resolved_rows:
        _guarantee_campaign_consumer(conn, tenant_id, approval_id, decision)
        _wake_waiting_workflow(conn, tenant_id, approval_id)
    return True


def _wake_waiting_workflow(conn: Any, tenant_id: UUID | str, approval_id: UUID | str) -> None:
    """VT-671 — WAKE the workflow parked on this approval the instant it resolves (ANY decision:
    an approval routes the execution leg, a decline routes the honest-decline leg — both should
    happen NOW, not on the next poll tick).

    Reads the ``wait_workflow_id`` stamped at park time (``task_store.park_awaiting_approval`` —
    the LIVE, possibly redrive-suffixed id) via the VT-668 reverse join, then ``DBOS.send``s a
    content-free hint on the owner-signal topic. The waiting loop's ``DBOS.recv`` returns early and
    RE-CHECKS the DB condition — the signal is a hint, never an authority, so a missed/duplicate
    send changes nothing but latency. Best-effort: any failure falls back to the poll ladder.
    """
    try:
        from dbos import DBOS

        from orchestrator.manager import task_store

        bound = task_store.find_task_for_resolved_approval(tenant_id, approval_id, conn=conn)
        if bound is None:
            return
        meta = bound.get("stall_metadata") or {}
        wf_id = meta.get("wait_workflow_id") if isinstance(meta, dict) else None
        if not wf_id:
            return  # pre-VT-671 park (no stamp) — the poll ladder covers it
        DBOS.send(str(wf_id), "resolved", topic="owner_signal")
    except Exception:  # noqa: BLE001 — a wake failure must never unwind the resolution
        logger.warning(
            "VT-671 workflow wake failed (fail-soft — poll ladder covers) tenant=%s approval=%s",
            tenant_id, approval_id, exc_info=True,
        )


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


# VT-668 — approval types whose executor is a manager-loop task the resolution seam must guarantee
# a live consumer for. A ``campaign_send`` is armed by the loop (workflow._dispatch_specialist_step,
# run_type='manager_dispatch') and executed by the loop's OWN approved-branch — so if that loop dies
# (a Railway redeploy strands its DBOS workflow; the ~6h retry ladder then reaps the task to
# dead_letter, all long before the 48h approval TTL), the owner's eventual approval resolves into
# SILENCE. Other types don't route through the loop: agent_customer_send owns its own durable send
# workflow (start_l2_send_for_resolved_approval), business_policy_grant/autonomy_upgrade have no send.
_LOOP_CONSUMER_APPROVAL_TYPES = frozenset({"campaign_send"})


def _guarantee_campaign_consumer(
    conn: Any, tenant_id: UUID | str, approval_id: UUID | str, decision: str
) -> None:
    """VT-668 — the money-path anti-silence guarantee at the single resolution choke point. When the
    owner APPROVES a ``campaign_send`` whose executing manager_task is DEAD (retry-ladder terminal or
    reaper-parked 'blocked'), the loop that would react is gone, so the approval otherwise resolves
    into SILENCE — the worst money-path trust shape. This finds the bound task (task_store.
    find_task_for_resolved_approval — the loop's run_id is a one-way hash, so the join rides the
    approval-park stamp / legacy source_message_ref) and, when it is dead, UN-STICKS it
    (``redrive_task``, the VT-557 operator primitive) and sends the owner an HONEST reply — never
    silence, never a false "sent" claim.

    Scope: ONLY an ``approved`` ``campaign_send``. A task in an ACTIVE/waiting state
    (running/verifying/waiting_owner/clarifying) is left for its (presumed live) loop — the reaper's
    orphaned-awaiting-approval sweep is the backstop if that loop is actually dead. Runs on the
    resolve ``conn`` so the redrive commits atomically with the resolution. FULLY FAIL-SOFT: the
    owner's authoritative resolution must never be unwound by a consumer-guarantee error (Pillar 7).

    Deliberately does NOT auto-send the customer campaign from here: a send at this seam would race a
    possibly-live loop (money-path double-send risk), so the honest reply directs the owner to
    re-trigger instead — the send is never claimed unless it actually happened (VT-668 no-drift)."""
    if decision != "approved":
        return
    try:
        from orchestrator.manager import task_store

        bound = task_store.find_task_for_resolved_approval(tenant_id, approval_id, conn=conn)
        if bound is None or bound.get("approval_type") not in _LOOP_CONSUMER_APPROVAL_TYPES:
            return  # no bound loop task (legacy graph-resume owns its own run), or not a loop send
        status = str(bound["status"])
        task_id = bound["id"]
        if status in ("dead_letter", "blocked"):
            task_store.redrive_task(tenant_id, task_id, conn=conn)
            _ack_owner_stalled_campaign(conn, tenant_id, reset=True)
        elif status in ("completed", "failed", "cancelled"):
            _ack_owner_stalled_campaign(conn, tenant_id, reset=False)
        # else running/verifying/waiting_owner/clarifying: a live loop reacts (unchanged path).
    except Exception:  # noqa: BLE001 — the consumer guarantee must never unwind the resolution
        logger.warning(
            "VT-668 consumer-guarantee failed (fail-soft) tenant=%s approval=%s",
            tenant_id, approval_id, exc_info=True,
        )


def _ack_owner_stalled_campaign(conn: Any, tenant_id: UUID | str, *, reset: bool) -> None:
    """VT-668 — the HONEST owner reply when an approved ``campaign_send`` resolves onto a dead
    executor. NEVER claims the send happened (the redriven task's dispatch step is already 'done',
    so the loop cannot auto-resume the send). Free-form (the owner just replied ⇒ inside the 24h
    window) via the SAME ``send_freeform_message`` primitive the owner-notification path uses — no
    new transport. FULLY FAIL-SOFT: an ack-send failure must never unwind the resolution. CL-390:
    no owner phone / body logged."""
    try:
        row = conn.execute(
            "SELECT owner_phone FROM tenants WHERE id = %s", (str(tenant_id),)
        ).fetchone()
        owner_phone = (row["owner_phone"] if isinstance(row, dict) else row[0]) if row else None
        if not owner_phone:
            logger.warning("VT-668 stalled-campaign ack: no owner_phone tenant=%s", tenant_id)
            return
        if reset:
            body = (
                "Got your approval — but this campaign had stalled while it was waiting, so I "
                "couldn't send it just now. Please send the request again and I'll set it up and "
                "send it right away."
            )
        else:
            body = (
                "Got your approval — but this campaign had already been closed by the time it "
                "arrived. Please send the request again and I'll prepare it fresh for you."
            )
        from orchestrator.utils.twilio_send import send_freeform_message

        send_freeform_message(body, owner_phone, tenant_id=tenant_id, surface="manager")
    except Exception:  # noqa: BLE001 — fail-soft: the owner ack never unwinds the resolution
        logger.warning(
            "VT-668 stalled-campaign ack send failed (fail-soft) tenant=%s", tenant_id, exc_info=True
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
