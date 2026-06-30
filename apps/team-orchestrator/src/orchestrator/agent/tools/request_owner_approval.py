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
from datetime import UTC, datetime, timedelta
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


class RequestOwnerApprovalError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class PauseRequestResult(BaseModel):
    """Outcome of the pause-request side effects (before the interrupt).

    status='armed'   -> template sent (or dry-run) + pending_approvals row
                        present; the caller should now interrupt().
    status='error'   -> template send failed; NO pending_approvals row written;
                        the caller must NOT interrupt (Pillar 7: no orphan pause).
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
      1.  Send the approval template to the OWNER. On error -> return error
          envelope and write NO pending_approvals row (so the caller will NOT
          interrupt; the run terminates as a normal error, not a stuck pause).
      2.  INSERT pending_approvals (decision NULL, status='pending',
          timeout_at = now + timeout_hours).

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

        # 1. Send the approval template to the owner.
        template_name = payload.template_name or APPROVAL_TEMPLATE_NAME
        if not dry_run:
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
                return PauseRequestResult(
                    status="error",
                    error=RequestOwnerApprovalError(
                        code=getattr(result, "error_code", None) or "template_send_failed",
                        message=getattr(result, "error_message", None) or "send failed",
                    ),
                )
            owner_message_sid = getattr(result, "message_sid", None)
        else:
            owner_message_sid = None

        # 2. INSERT the pending_approvals row (decision NULL).
        approval_id = uuid4()
        timeout_at = datetime.now(UTC) + timedelta(hours=payload.timeout_hours)
        from psycopg.errors import UniqueViolation
        from psycopg.types.json import Jsonb

        row: dict[str, Any] = {
            "id": str(approval_id),
            "run_id": str(run_id),
            "campaign_id": str(payload.campaign_id) if payload.campaign_id else None,
            "approval_type": payload.approval_type,
            "summary": payload.summary,
            "details": Jsonb(dict(payload.details)),
            "status": "pending",
            "decision": None,
            "owner_message_sid": owner_message_sid,
            "timeout_at": timeout_at,
        }
        if payload.draft_batch_id is not None:
            row["draft_batch_id"] = str(payload.draft_batch_id)

        # VT-306: insert through the typed wrapper on the caller's conn (atomic
        # with the surrounding arm-pause txn; tenant_id forced to the scope).
        try:
            PendingApprovalsWrapper().insert(tenant_id, row, conn=conn)
        except UniqueViolation:
            # VT-369 §4.1 race-loser path: a concurrent armer won between the
            # step-0b check and this INSERT — the migration-128 partial unique
            # index (one open row per tenant) rejected ours. Same refusal as 0b.
            # (Accepted residual: the owner template above already went out; the
            # owner's reply resolves the WINNER's row — Pillar-7-safe, no orphan
            # pause because no row of ours exists.)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 — autocommit conn: nothing pending
                pass
            logger.info(
                "request_owner_approval: refused (one-open-per-tenant race lost) "
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
