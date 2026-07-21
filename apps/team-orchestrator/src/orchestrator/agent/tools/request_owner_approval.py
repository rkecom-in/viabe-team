"""VT-47 — request_owner_approval: the Pillar-7 pause/resume gate.

This is the AUTHORITATIVE approval gate for sensitive actions (campaign
sends, cohort-size-exceeded, sensitive-data access). The agent CANNOT
bypass it: the side effect it gates (e.g. a campaign send) is structurally
downstream of the owner's decision, which only arrives via resume.

Architecture (Pillar 1 — the orchestrator owns the pause/resume state
machine; the gate only EMITS the pause)
---------------------------------------------------------------------------
The pause is a LangGraph ``interrupt()`` call made from inside a LangGraph
node (``request_owner_approval_node``), so it MUST run on a graph compiled
with a checkpointer (PostgresSaver). ``interrupt()`` raises ``GraphInterrupt``
internally; LangGraph's pregel loop catches it, persists the checkpoint at
the interrupting node, and surfaces ``{"__interrupt__": (...)}`` to the
caller of ``graph.invoke``. dispatch.py reads that key and maps it to a
``paused`` terminal (it does NOT see a raw exception — verified empirically
against langgraph==1.2.0).

On resume the interrupting node RE-EXECUTES from its start (langgraph
``interrupt()`` docstring, types.py:801-813). So the send-template + INSERT
effects MUST be idempotent across re-execution. We guard them: if an OPEN
pending_approvals row already exists for this run, the pause primitive does
NOT re-send / re-insert; it just re-arms the interrupt. After resume the
node reads the resolved decision from the resume payload (and cross-checks
the durable row) and returns it as state — it never re-sends.

Owner-send contract (CONTRACT DECISION, VT-47)
---------------------------------------------------------------------------
The brief says "send the team_weekly_approval template via VT-45". VT-45's
``send_whatsapp_template`` is CUSTOMER-targeted: it requires a customer_id,
resolves a customers-row phone, and HARD-REFUSES opted-out/blocked
recipients (CL-421). An owner approval request is OWNER-targeted — the
owner has no customers row and cannot be "opted out" of approval prompts.
Forcing it through VT-45 would be a semantic + CL-421 mismatch. So we route
through the lower-level primitive VT-45 itself wraps:
``orchestrator.utils.twilio_send.send_template_message(tenant_id,
template_name, params, recipient_phone=owner_phone)`` — the owner phone
resolves from ``tenants.owner_phone`` (migration 050), falling back to the
tenant's whatsapp_number (send_template_message's own default).

CL-390: log approval_id + tenant_id + approval_type + decision ONLY — never
the message body, never the owner phone. owner_message_sid (a Twilio SID) is
allowed.
CL-422: dev = synthetic data only until prod-Mumbai (VT-231).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Literal
from uuid import UUID, uuid4

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.db.wrappers import PendingApprovalsWrapper
from orchestrator.state.agent_graph_state import AgentGraphState

logger = logging.getLogger(__name__)

# The Phase-1 owner-approval template (VT-163 registry name; resolves
# name+lang -> content_sid). Brief D4: team_weekly_approval (NOT the legacy
# weekly_approval_request).
APPROVAL_TEMPLATE_NAME = "team_weekly_approval"

# VT-683 P2c — the IN-SESSION interactive approval ask (twilio/quick-reply decision buttons;
# registry entry team_approval_buttons, en+hi). Sent INSTEAD of the Meta template when the owner's
# 24h session is open; the template remains the out-of-window belt. The button TITLES are part of
# the resolution contract (they echo back as the inbound Body and must classify deterministically) —
# see the registry entry + canaries/vt683_approval_buttons_create.py before changing anything.
INTERACTIVE_APPROVAL_TEMPLATE = "team_approval_buttons"

_DEFAULT_TIMEOUT_HOURS = 48
_MAX_TIMEOUT_HOURS = 168  # 7 days

ApprovalType = Literal[
    "campaign_send", "cohort_size_exceeded", "sensitive_data_access", "other",
    # VT-369 Gap-5 — the agent customer-messaging surface. CL-428: this Literal is the
    # source of truth — migration 128 keeps the DB CHECK in exact sync (all three added
    # in both, same PR; VT-384 PR-3 activates autonomy_upgrade — the offer/ENABLE consent row).
    "agent_customer_send", "autonomy_upgrade", "l3_presend_notice",
    # VT-467 — the business-impact rails surface (SPEND / COMMITMENT / CONFIG action above the
    # tenant's autonomy threshold). CL-428: this Literal is the source of truth — migration 143
    # keeps the DB CHECK in exact sync (added in both, same PR). The business-impact gate routes its
    # owner-approval ask through the SAME arm_pause_request path agent_customer_send uses.
    "business_impact_action",
    # VT-609 fix round — the onboarding-conductor's business-policy PROPOSAL (the specialist can
    # PROPOSE machine-enforceable bounds; only the owner's resolution grants them). CL-428:
    # migration 169 keeps the DB CHECK in exact sync (added in both, same PR). Does NOT route
    # through arm_pause_request (no registered WhatsApp template for this ask exists/is authorized
    # yet — the specialist's own conversational reply carries the ask); armed via
    # ``business_policy.propose_business_policy_grant`` instead. See that module for the full
    # arm/resolve shape (mirrors business_impact_choke.dispatch_autonomy_offer/resolve_and_grant_l3).
    "business_policy_grant",
]
# The raw owner decision verb recorded on pending_approvals.decision. CL-428: this Literal is
# the source of truth — migration 110 keeps the DB CHECK in exact sync ('defer' added in both,
# same PR — VT-334).
Decision = Literal["approved", "rejected", "needs_changes", "timeout", "defer"]


class RequestOwnerApprovalInput(BaseModel):
    """Typed input for the pause primitive (frozen)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    run_id: UUID
    approval_type: ApprovalType
    summary: str = Field(..., min_length=1, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)
    campaign_id: UUID | None = None
    template_params: dict[str, str] = Field(default_factory=dict)
    language: Literal["en", "hi"] = "en"
    timeout_hours: int = Field(default=_DEFAULT_TIMEOUT_HOURS, ge=1, le=_MAX_TIMEOUT_HOURS)
    # VT-369: the agent surface links the approval to its draft batch (migration 128
    # column; ON DELETE SET NULL is safe because the row carries NO customer PII —
    # batch id + counts only, the binding no-PII-in-approvals rule).
    draft_batch_id: UUID | None = None
    # VT-369: the agent surface sends `team_agent_draft_approval` instead of the weekly
    # template. None -> APPROVAL_TEMPLATE_NAME (the legacy default, unchanged).
    template_name: str | None = None
    # VT-594 (post-review restructure) — a PII-safe {"en": ..., "hi": ...} plan-summary
    # body, sent BEST-EFFORT before the approval template (see arm_pause_request). None
    # for every existing caller that doesn't build one (agent_customer_send,
    # business_impact_choke, autonomy) — no behavior change for them.
    chat_summary: dict[str, str] | None = None
    # T9 inc-3 — the manager task this approval settles (enforce path only; the node
    # threads it from graph state). Anchors the stale-turn check: if the owner has sent
    # a NEWER inbound since the turn that spawned this task (manager_tasks.
    # source_message_ref), the chat_summary gets the reconciled "earlier request"
    # framing so the late delivery reads as a promised follow-through, not a pile-on
    # onto an unrelated later turn. None (legacy/weekly callers) → no check, unchanged.
    manager_task_id: UUID | None = None


class RequestOwnerApprovalError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class PauseRequestResult(BaseModel):
    """Outcome of the pause-request side effects (before the interrupt).

    status='armed'   -> pending_approvals row INSERTed then template sent (or
                        dry-run); the caller should now interrupt().
    status='error'   -> template send failed; the armed row is DELETEd (VT-615
                        arm-then-send rollback) so NO open row remains; the caller
                        must NOT interrupt (Pillar 7: no orphan pause).
    status='refused' -> VT-369 §4.1 (F5): ANOTHER approval is already open for this
                        tenant — the approval queue is serialized per tenant so two
                        open rows can never race one owner "yes" onto the wrong
                        surface. NO row written, NO interrupt. Agent callers treat
                        this as defer-to-next-sweep; weekly-cadence callers retry on
                        their own schedule.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["armed", "error", "refused"]
    approval_id: UUID | None = None
    error: RequestOwnerApprovalError | None = None


# A template sender: (tenant_id, template_name, params, *, recipient_phone) -> SendResult-like.
TemplateSender = Callable[..., Any]


def _resolve_owner_phone(conn: Any, tenant_id: UUID) -> str | None:
    """Return the owner approval recipient phone for the tenant.

    Prefers ``tenants.owner_phone`` (the owner's personal mobile anchor,
    migration 050); falls back to None so send_template_message's own
    default (whatsapp_number) applies. Tenant-scoped read (RLS via the
    open tenant_connection).
    """
    row = conn.execute(
        "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s",
        (str(tenant_id),),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        phone = row.get("owner_phone") or row.get("whatsapp_number")
    else:
        phone = row[0] or row[1]
    return str(phone) if phone else None


def _find_open_approval(conn: Any, tenant_id: UUID, run_id: UUID) -> dict[str, Any] | None:
    """Return the most-recent UNRESOLVED approval row for this tenant/run, else None.
    VT-306: reads through the typed wrapper on the caller's conn."""
    return PendingApprovalsWrapper().find_open_for_run(tenant_id, run_id, conn=conn)


# T9 inc-3 — the reconciled framing for a STALE settle (the owner sent a newer message while the
# async task was still drafting). Prefixed to the chat_summary so the late delivery reads as the
# promised follow-through on the EARLIER request, not a non-sequitur pile-on onto whatever turn is
# current. Locale-keyed; the resolved-locale text gets the matching prefix (en fallback).
_STALE_DRAFT_PREFIX = {
    "en": "About your earlier campaign request — here's the draft, ready for your review.\n\n",
    "hi": "आपके पहले वाले कैंपेन अनुरोध की बात — ड्राफ़्ट तैयार है, आपकी समीक्षा के लिए।\n\n",
}


def _owner_sent_newer_message(conn: Any, tenant_id: UUID, manager_task_id: UUID) -> bool:
    """T9 inc-3 — True iff the owner sent a NEWER inbound after the turn that spawned this manager
    task. The anchor is manager_tasks.source_message_ref (the spawning inbound's Twilio sid, written
    by plan_store.create_plan), joined to its conversation_log owner row; the role-flipped twin of
    runner._brain_emitted_owner_reply. A missing anchor (NULL ref / no matching log row) compares
    NULL → EXISTS false → NOT stale. Fail-soft False: a read error must only ever fall back to
    today's framing — it never touches the arm or the template send (Pillar 7 untouched)."""
    try:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM conversation_log o
                WHERE o.tenant_id = %s AND o.role = 'owner'
                  AND o.created_at > (
                      SELECT s.created_at FROM conversation_log s
                      JOIN manager_tasks mt
                        ON mt.tenant_id = s.tenant_id AND mt.source_message_ref = s.message_sid
                      WHERE mt.id = %s AND s.tenant_id = %s AND s.role = 'owner'
                      ORDER BY s.created_at DESC LIMIT 1
                  )
            ) AS stale
            """,
            (str(tenant_id), str(manager_task_id), str(tenant_id)),
        ).fetchone()
    except Exception:  # noqa: BLE001 — framing-only read; never block the arm on it
        logger.warning(
            "request_owner_approval: stale-turn check failed (fail-soft → default framing) "
            "tenant=%s task=%s", tenant_id, manager_task_id,
        )
        return False
    if row is None:
        return False
    return bool(row["stale"] if isinstance(row, dict) else row[0])


def arm_pause_request(
    payload: RequestOwnerApprovalInput,
    *,
    conn_factory: Callable[..., Any] | None = None,
    send_fn: TemplateSender | None = None,
    dry_run: bool = False,
) -> PauseRequestResult:
    """Perform the pause side effects: send the approval template + INSERT the
    pending_approvals row. Idempotent across resume re-execution.

    Order of effects (Pillar 7 — no orphan pause):
      0.  If an OPEN approval already exists for this run, this is a resume
          re-execution: do NOT re-send, do NOT re-insert; return armed with the
          existing approval_id.
      0b. VT-369 §4.1 (F5) — per-tenant approval-queue serialization: if ANY
          open approval exists for the tenant (necessarily a DIFFERENT run —
          step 0 already returned for this run's), REFUSE to arm (status=
          'refused', no send, no row). Two open rows + one owner "yes" would
          resolve the wrong surface (find_open_for_tenant is newest-LIMIT-1,
          blind to approval_type). The migration-128 partial unique index
          (one open row per tenant) is the structural backstop — its
          IntegrityError at step 2 is the race-loser path, same refusal.
      1.  INSERT pending_approvals (decision NULL, status='pending', timeout_at =
          NULL — VT-683 POINT A: the decision clock starts at DELIVERY, step 2d) —
          the DURABLE row FIRST (VT-615 arm-then-send). A migration-128
          one-open-per-tenant unique race is lost HERE (UniqueViolation) BEFORE any
          send: status='refused', no ask out, no dropped campaign.
      2.  Write the owner_comms_queue delivery-ledger record (fail-soft), send the
          plan summary (best-effort), then the LOAD-BEARING ask: the in-session
          INTERACTIVE quick-reply when the 24h session is open (VT-683 P2c), else
          the Meta approval template (the out-of-window belt). On total send error
          -> DELETE the row just armed + drop the ledger record (restores "error ->
          no open row") and return the error envelope; the caller will NOT
          interrupt, the run terminates as a normal error, not a stuck pause.
          On success -> start the decision clock (timeout_at = now + timeout_hours)
          + mark the ledger record delivered (POINT A: the owner can't time out on
          an ask he never saw).

    ``conn_factory`` defaults to orchestrator.db.tenant_connection.
    ``send_fn`` defaults to twilio_send.send_template_message.
    ``dry_run`` skips the real Twilio call (canary/CI default at the node).
    """
    from orchestrator.observability.tm_audit import emit_tm_audit
    if conn_factory is None:
        from orchestrator.db import tenant_connection

        conn_factory = tenant_connection
    if send_fn is None:
        from orchestrator.utils.twilio_send import send_template_message

        send_fn = send_template_message

    tenant_id = payload.tenant_id
    run_id = payload.run_id

    with conn_factory(tenant_id) as conn:
        # 0. Idempotency guard — resume re-executes the node from its start.
        existing = _find_open_approval(conn, tenant_id, run_id)
        if existing is not None:
            logger.info(
                "request_owner_approval: open approval already present "
                "(resume re-exec) tenant=%s run=%s approval=%s type=%s",
                tenant_id, run_id, existing["id"], payload.approval_type,
            )
            return PauseRequestResult(
                status="armed", approval_id=UUID(existing["id"])
            )

        # 0b. VT-369 §4.1 (F5) — per-tenant queue serialization: any OTHER open
        # approval for this tenant refuses the arm (defer-to-next-cycle for the
        # caller; no send, no row, no interrupt).
        open_any = PendingApprovalsWrapper().find_open_for_tenant(tenant_id, conn=conn)
        if open_any is not None:
            logger.info(
                "request_owner_approval: refused (queue busy) tenant=%s run=%s "
                "type=%s open_approval=%s open_type=%s",
                tenant_id, run_id, payload.approval_type,
                open_any["id"], open_any.get("approval_type"),
            )
            return PauseRequestResult(
                status="refused",
                error=RequestOwnerApprovalError(
                    code="approval_queue_busy",
                    message=(
                        "Another approval is already open for this tenant — the "
                        "approval queue is serialized per tenant (VT-369 §4.1/F5). "
                        "Retry on the next cycle/sweep."
                    ),
                ),
            )

        owner_phone = _resolve_owner_phone(conn, tenant_id)

        # VT-615 (A) — ARM-THEN-SEND (was send-then-INSERT). The pending_approvals row is the durable
        # source of truth for the owner's eventual "haan bhej do"; it MUST exist BEFORE any owner-facing
        # send. The prior order sent the summary + approval template FIRST and INSERTed second, so a lost
        # migration-128 one-open-per-tenant unique race (or any INSERT failure) left a template already in
        # the owner's hand with NO row to resolve — the owner approves, find_open returns None, the reply
        # falls through to dispatch_brain, and the campaign NEVER SENDS (the old "accepted residual"). Now:
        # INSERT first (a race-loser refuses BEFORE any send — no phantom template, no dropped campaign);
        # the sends happen only once the row is durable; a send failure DELETEs the row (autocommit conn —
        # no implicit rollback; the pending_approvals_delete RLS policy permits the tenant-scoped delete) so
        # no orphan blocks the tenant's one-open queue and the "error -> no open row" contract still holds.
        approval_id = uuid4()
        from psycopg.errors import UniqueViolation
        from psycopg.types.json import Jsonb

        # VT-683 POINT A (Fazal 2026-07-21): the decision clock starts at DELIVERY, never at arm.
        # timeout_at is inserted NULL (mig 179) and set by start_decision_clock the moment the ask
        # is actually SENT to the owner (step 2d below). The timeout sweep skips NULL rows, so an
        # undelivered ask can never be reaped as 'timed_out'; the sweep's NULL-clock belt (24h
        # grace) reaps a crash-orphaned arm instead.
        row: dict[str, Any] = {
            "id": str(approval_id),
            "run_id": str(run_id),
            "campaign_id": str(payload.campaign_id) if payload.campaign_id else None,
            "approval_type": payload.approval_type,
            "summary": payload.summary,
            "details": Jsonb(dict(payload.details)),
            "status": "pending",
            "decision": None,
            "owner_message_sid": None,  # set after the send succeeds (step 2c)
            "timeout_at": None,
        }
        if payload.draft_batch_id is not None:
            row["draft_batch_id"] = str(payload.draft_batch_id)

        # 1. INSERT the durable row FIRST. A migration-128 race-loser refuses HERE, before any send —
        #    no phantom template reaches the owner, so no campaign is silently dropped.
        try:
            PendingApprovalsWrapper().insert(tenant_id, row, conn=conn)
        except UniqueViolation:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 — autocommit conn: nothing pending
                pass
            logger.info(
                "request_owner_approval: refused (one-open-per-tenant race lost, PRE-send) "
                "tenant=%s run=%s type=%s",
                tenant_id, run_id, payload.approval_type,
            )
            return PauseRequestResult(
                status="refused",
                error=RequestOwnerApprovalError(
                    code="approval_queue_busy",
                    message=(
                        "Lost the one-open-approval-per-tenant race (unique index, "
                        "migration 128). Retry on the next cycle/sweep."
                    ),
                ),
            )

        # VT-683 P2c — the delivery-ledger record (owner_comms_queue, mig 178), written on the SAME
        # conn right after the arm. decision_ref links it to the money authority (pending_approvals);
        # mark_delivered at step 2d starts the queue-side deadline mirror. FAIL-SOFT: the ledger is
        # tracking, never authority — a failed enqueue must not block the arm (the timeout sweep's
        # NULL-clock belt still covers a row whose delivery bookkeeping is missing).
        template_name = payload.template_name or APPROVAL_TEMPLATE_NAME
        from orchestrator.owner_surface import owner_comms_queue as _comms_q

        queue_item_id: UUID | None = None
        try:
            queue_item_id = _comms_q.enqueue(
                tenant_id,
                kind="approval",
                payload={"text": payload.summary, "fallback_template": template_name},
                decision_ref={"kind": "pending_approval", "id": str(approval_id)},
                conn=conn,
            )
        except Exception:  # noqa: BLE001 — ledger only; never block the arm
            logger.warning(
                "request_owner_approval: comms-ledger enqueue failed (fail-soft) tenant=%s run=%s",
                tenant_id, run_id,
            )

        # A send failure past this point must remove the row we just armed (autocommit already committed
        # it), else the orphan blocks the tenant's one-open queue until the timeout sweep reaps it.
        def _rollback_arm() -> None:
            try:
                PendingApprovalsWrapper().delete_by_id(tenant_id, approval_id, conn=conn)
            except Exception:  # noqa: BLE001 — best-effort; the timeout sweep is the backstop
                # ERROR (not warning): a swallowed rollback leaves a committed open row that blocks THIS
                # tenant's one-open approval queue until the timeout sweep reaps it (the NULL-clock belt,
                # 24h grace + sweep tick). Rare — needs an independent DB-conn death between INSERT and
                # rollback (the send fails over HTTP, so the conn is normally healthy) — but must not be
                # silent.
                logger.error(
                    "request_owner_approval: arm rollback DELETE FAILED — tenant approval queue blocked "
                    "until the timeout sweep's NULL-clock belt reaps it tenant=%s run=%s approval=%s",
                    tenant_id, run_id, approval_id,
                )
            if queue_item_id is not None:
                try:
                    _comms_q.drop_item(
                        tenant_id, queue_item_id, reason="send_failed", conn=conn
                    )
                except Exception:  # noqa: BLE001 — ledger hygiene only
                    logger.warning(
                        "request_owner_approval: comms-ledger drop failed (fail-soft) "
                        "tenant=%s run=%s", tenant_id, run_id,
                    )

        # 2a. Best-effort PII-safe plan-summary send BEFORE the approval template (VT-594), so the owner
        #     sees WHAT they're approving. A summary-send failure must NEVER block the arm.
        if not dry_run and payload.chat_summary:
            try:
                from orchestrator.owner_surface.freeform_acks import (
                    resolve_owner_locale,
                    send_freeform_ack,
                )

                if owner_phone:
                    locale = resolve_owner_locale(tenant_id)
                    text = payload.chat_summary.get(locale) or payload.chat_summary.get("en")
                    # T9 inc-3 — stale settle (owner moved on to a newer turn while the async task
                    # drafted): reframe as the promised follow-through on the EARLIER request.
                    # TEXT-ONLY: the arm row above and the template send below are untouched.
                    if (
                        text
                        and payload.manager_task_id is not None
                        and _owner_sent_newer_message(conn, tenant_id, payload.manager_task_id)
                    ):
                        text = (_STALE_DRAFT_PREFIX.get(locale) or _STALE_DRAFT_PREFIX["en"]) + text
                        logger.info(
                            "request_owner_approval: stale-turn reconcile framing applied "
                            "tenant=%s run=%s task=%s",
                            tenant_id, run_id, payload.manager_task_id,
                        )
                    if text:
                        send_freeform_ack(tenant_id, owner_phone, text)
            except Exception:  # noqa: BLE001 — best-effort; must never block the arm
                logger.warning(
                    "request_owner_approval: chat-summary send failed (fail-soft) "
                    "tenant=%s run=%s", tenant_id, run_id,
                )

        # 2b. THE LOAD-BEARING SEND (VT-683 P2c). In-window: the in-session INTERACTIVE ask
        #     (team_approval_buttons quick-reply — decision buttons, no Meta template; the button
        #     TITLE echoes back as the inbound Body and classify_approval_reply resolves it against
        #     THIS row — the one-open-per-tenant serialization above is what guarantees "same row").
        #     Out-of-window, interactive failure, or fail-closed session read: the Meta approval
        #     template, byte-identical to the pre-P2c path (the belt until P3 wake-up + P4
        #     whitelist retire it). On total failure, roll back the arm row so the owner never
        #     holds an ask with no resolvable row.
        owner_message_sid: str | None = None
        delivered_channel: str | None = None
        if not dry_run:
            interactive_sid: str | None = None
            try:
                from orchestrator.owner_surface.session_window import session_open

                if owner_phone and session_open(tenant_id):
                    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale
                    from orchestrator.templates_registry import content_sid_for
                    from orchestrator.utils.twilio_send import send_interactive_message

                    locale = resolve_owner_locale(tenant_id)
                    content_sid = content_sid_for(
                        INTERACTIVE_APPROVAL_TEMPLATE,
                        locale if locale in ("en", "hi") else "en",
                    )
                    if content_sid:
                        interactive_sid = send_interactive_message(
                            content_sid,
                            owner_phone,
                            content_variables={"1": payload.summary},
                            tenant_id=tenant_id,
                            surface="manager",
                        )
            except Exception as exc:  # noqa: BLE001 — interactive is the preferred channel, never the only one
                logger.warning(
                    "request_owner_approval: in-session interactive send failed — falling back to "
                    "template tenant=%s run=%s type=%s err=%s",
                    tenant_id, run_id, payload.approval_type, type(exc).__name__,
                )
            if interactive_sid:
                owner_message_sid = interactive_sid
                delivered_channel = "interactive_session"
            else:
                try:
                    result = send_fn(
                        tenant_id,
                        template_name,
                        dict(payload.template_params),
                        recipient_phone=owner_phone,
                    )
                except Exception as exc:  # noqa: BLE001 — never leak; honest envelope
                    logger.warning(
                        "request_owner_approval: template send raised "
                        "tenant=%s run=%s type=%s err=%s",
                        tenant_id, run_id, payload.approval_type, type(exc).__name__,
                    )
                    _rollback_arm()
                    return PauseRequestResult(
                        status="error",
                        error=RequestOwnerApprovalError(
                            code="template_send_failed", message=type(exc).__name__
                        ),
                    )
                if not getattr(result, "success", False):
                    logger.warning(
                        "request_owner_approval: template send unsuccessful "
                        "tenant=%s run=%s type=%s code=%s",
                        tenant_id, run_id, payload.approval_type,
                        getattr(result, "error_code", None),
                    )
                    _rollback_arm()
                    return PauseRequestResult(
                        status="error",
                        error=RequestOwnerApprovalError(
                            code=getattr(result, "error_code", None) or "template_send_failed",
                            message=getattr(result, "error_message", None) or "send failed",
                        ),
                    )
                owner_message_sid = getattr(result, "message_sid", None)
                delivered_channel = "template"
            # 2c. Record which owner message carried the ask (metadata; resolve COALESCEs it).
            if owner_message_sid:
                try:
                    PendingApprovalsWrapper().set_owner_message_sid(
                        tenant_id, approval_id, owner_message_sid, conn=conn
                    )
                except Exception:  # noqa: BLE001 — metadata only; never fail the arm on it
                    logger.warning(
                        "request_owner_approval: owner_message_sid UPDATE failed (non-critical) "
                        "tenant=%s run=%s approval=%s", tenant_id, run_id, approval_id,
                    )

        # 2d. POINT A — the ask is now DELIVERED (or dry_run pretends it is): start the decision
        #     clock (timeout_at = now + ttl; the arm inserted NULL) and mark the ledger record
        #     delivered (queue-side decision_deadline_at mirror). BOTH are fail-soft: the sweep's
        #     NULL-clock belt covers a missed clock start; the ledger is never authority. dry_run
        #     starts the clock at arm (the canary/CI contract expects an armed row with a running
        #     clock, and nothing was actually queued for later delivery).
        try:
            PendingApprovalsWrapper().start_decision_clock(
                tenant_id, approval_id, timeout_hours=payload.timeout_hours, conn=conn
            )
        except Exception:  # noqa: BLE001 — the NULL-clock belt reaps a row whose clock never started
            logger.error(
                "request_owner_approval: decision-clock start FAILED (belt will reap) "
                "tenant=%s run=%s approval=%s", tenant_id, run_id, approval_id,
            )
        if queue_item_id is not None:
            try:
                _comms_q.mark_delivered(
                    tenant_id,
                    queue_item_id,
                    kind="approval",
                    message_sid=owner_message_sid,
                    decision_ttl=timedelta(hours=payload.timeout_hours),
                    conn=conn,
                )
            except Exception:  # noqa: BLE001 — ledger only
                logger.warning(
                    "request_owner_approval: comms-ledger mark_delivered failed (fail-soft) "
                    "tenant=%s run=%s", tenant_id, run_id,
                )

        emit_tm_audit(
            event_layer="does",
            event_kind="approval_armed",
            actor="team_manager",
            tenant_id=tenant_id,
            run_id=run_id,
            action={
                "approval_id": str(approval_id),
                "approval_type": payload.approval_type,
                "draft_batch_id": str(payload.draft_batch_id) if payload.draft_batch_id else None,
                "dry_run": dry_run,
                # VT-683 P2c: which channel carried the ask ('interactive_session' | 'template' |
                # None on dry_run) — the in-window/out-of-window observability seam.
                "channel": delivered_channel,
            },
            summary=f"approval armed: {payload.approval_type}",
            conn=conn,
        )

    logger.info(
        "request_owner_approval: armed tenant=%s run=%s approval=%s type=%s "
        "timeout_h=%d dry_run=%s",
        tenant_id, run_id, approval_id, payload.approval_type,
        payload.timeout_hours, dry_run,
    )
    return PauseRequestResult(status="armed", approval_id=approval_id)


def request_owner_approval_node(state: AgentGraphState) -> dict[str, Any]:
    """LangGraph node: the Pillar-7 approval gate.

    Reads the approval request the collapse path attached to state
    (``state['pending_approval_request']``), arms the pause (send template +
    INSERT pending_approvals), then calls ``interrupt()`` to halt the graph.

    On resume, the node RE-EXECUTES from the top: ``arm_pause_request`` is a
    no-op (the open row already exists), ``interrupt()`` returns the resume
    payload, and the node returns the resolved decision into state.

    The decision lands at ``state['owner_decision']``. Downstream consumers
    (the campaign-send path, dispatch terminal classifier) read it; a
    non-'approved' decision MUST NOT proceed to send (Pillar 7).
    """
    req = state.get("pending_approval_request")
    if req is None:
        raise RuntimeError(
            "request_owner_approval_node: state['pending_approval_request'] "
            "is missing — the collapse path must attach it before routing here."
        )

    tenant_id = state.get("tenant_id")
    run_id = state.get("run_id")
    if tenant_id is None or run_id is None:
        raise RuntimeError(
            "request_owner_approval_node: tenant_id / run_id missing from state"
        )

    payload = RequestOwnerApprovalInput(
        tenant_id=tenant_id,
        run_id=run_id,
        approval_type=req.get("approval_type", "campaign_send"),
        summary=req.get("summary", "Owner approval required"),
        details=req.get("details", {}),
        campaign_id=req.get("campaign_id"),
        template_params=req.get("template_params", {}),
        language=req.get("language", "en"),
        timeout_hours=req.get("timeout_hours", _DEFAULT_TIMEOUT_HOURS),
        chat_summary=req.get("chat_summary"),
        # T9 inc-3 — enforce path only (legacy/weekly graphs carry no manager_task_id → None →
        # no stale-turn check). Anchors the reconciled framing for a late async settle.
        manager_task_id=state.get("manager_task_id"),
    )

    # dry_run is carried on the request so the canary / CI exercise the full
    # pause/resume without a live Twilio call (default False = production send).
    armed = arm_pause_request(payload, dry_run=bool(req.get("dry_run", False)))
    if armed.status == "refused":
        # VT-369 §4.1 (F5): another approval is already open for this tenant —
        # the queue is serialized per tenant. NO row was written and NO pause
        # fires. The campaign stays 'proposed'; the weekly cadence retries on its
        # own schedule. route_after_approval sends any non-'approved' decision to
        # END (Pillar 7: a refused arm can never proceed to send).
        return {
            "owner_decision": "queue_busy",
            "approval_error": armed.error.model_dump() if armed.error else None,
        }
    if armed.status == "error":
        # No pending_approvals row was written. Surface a clean terminal that
        # does NOT pause and does NOT send: the campaign is not approved, so
        # it must not proceed (Pillar 7). dispatch classifies this as a
        # completed run that did not send.
        return {
            "owner_decision": "send_failed",
            "approval_error": armed.error.model_dump() if armed.error else None,
        }

    # interrupt() halts the graph; the value is surfaced to the resume client.
    # On resume the call returns the Command(resume=...) payload.
    resume_value = interrupt(
        {
            "approval_id": str(armed.approval_id),
            "approval_type": payload.approval_type,
            "summary": payload.summary,
        }
    )

    # --- resumed past here ---
    decision = None
    if isinstance(resume_value, dict):
        decision = resume_value.get("decision")
    elif isinstance(resume_value, str):
        decision = resume_value

    logger.info(
        "request_owner_approval_node: resumed tenant=%s run=%s approval=%s decision=%s",
        tenant_id, run_id, armed.approval_id, decision,
    )
    return {"owner_decision": decision, "approval_id": armed.approval_id}


__all__ = [
    "APPROVAL_TEMPLATE_NAME",
    "ApprovalType",
    "Decision",
    "PauseRequestResult",
    "RequestOwnerApprovalError",
    "RequestOwnerApprovalInput",
    "arm_pause_request",
    "request_owner_approval_node",
]
