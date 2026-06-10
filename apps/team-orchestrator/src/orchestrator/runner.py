"""DBOS workflow entry point for an orchestrator pipeline run (VT-3.1).

Pillar 1: no reasoning here — the steps only persist run state and drive the
LangGraph substrate. Pillar 8: one workflow, one substrate.

Each ``@DBOS.step`` is a durable checkpoint. DBOS auto-resumes the workflow
from the last completed step after a crash. Steps are written idempotently so
recovery is safe.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout
from psycopg.types.json import Jsonb

from dbos_config import WORKFLOW_TIMEOUT_SECONDS
from orchestrator.db import tenant_connection
from orchestrator.direct_handlers import HANDLERS
from orchestrator.graph import OrchestratorState, get_compiled_graph
from orchestrator.memory.l0_writer import _owner_inputs_enabled
from orchestrator.owner_inputs import run_extraction_for_event
from orchestrator.pre_filter_gate import pre_filter
from orchestrator.state import new_subscriber_state
from orchestrator.types import WebhookEvent
from orchestrator.utils.phone_token import hash_phone

# SHIP GATE (VT-146 / CL 368387c2-cc5a-81ba): owner_inputs extraction
# transmits raw customer message bodies to the classifier vendor for
# structured-intent extraction. Must stay False until the vendor DPA +
# ZDR are executed and the privacy notice is signed (Fazal-owned).
# Flipping this is a reviewed code change by design — do not convert
# to an env var.
OWNER_INPUTS_EXTRACTION_ENABLED = False

logger = logging.getLogger(__name__)


def _brain_owner_inputs_ok(tenant_id: str) -> bool:
    """VT-303 / CL-425 — fail-closed owner_inputs consent check for the brain.

    The brain (dispatch_brain) transmits the owner's inbound body — which may
    carry customer PII — to Anthropic (sub-processor). ``owner_inputs`` is the
    lawful basis (CL-425). Any error reading the flag fails CLOSED (treat as not
    consented): we never transmit on an unknown consent state.
    """
    try:
        return _owner_inputs_enabled(UUID(tenant_id))
    except Exception:  # noqa: BLE001 — fail-closed on any consent-check error
        logger.warning(
            "VT-303: owner_inputs consent check failed (tenant=%s); fail-closed",
            tenant_id,
        )
        return False


@DBOS.step()
def open_run(tenant_id: str, run_id: str) -> None:
    """Record the run as started. Idempotent (ON CONFLICT) so recovery is safe."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running') "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id),
        )


@DBOS.step()
def invoke_graph(tenant_id: str, run_id: str, inbound: str) -> list[str]:
    """Run the LangGraph substrate for this run. thread_id == run_id."""
    state: OrchestratorState = {
        "tenant_id": UUID(tenant_id),
        "run_id": UUID(run_id),
        "history": [inbound],
    }
    result = get_compiled_graph().invoke(state, config={"configurable": {"thread_id": run_id}})
    return list(result["history"])


@DBOS.step()
def close_run(tenant_id: str, run_id: str) -> None:
    """Mark the run completed. Idempotent.

    tenant_id is required so the UPDATE runs under tenant_connection — under
    RLS the WHERE id = %s is scoped by the USING clause, so without the GUC
    set the UPDATE is a silent no-op (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = 'completed', ended_at = now() WHERE id = %s",
            (run_id,),
        )


@DBOS.workflow()
def pipeline_run(tenant_id: str, run_id: str, inbound: str) -> dict[str, Any]:
    """Durable orchestrator pipeline run — three checkpointed steps."""
    open_run(tenant_id, run_id)
    history = invoke_graph(tenant_id, run_id, inbound)
    close_run(tenant_id, run_id)
    return {"tenant_id": tenant_id, "run_id": run_id, "history": history}


def run_pipeline(tenant_id: str, run_id: str, inbound: str) -> dict[str, Any]:
    """Run ``pipeline_run`` durably, keyed on ``run_id`` for idempotency.

    The 6-minute timeout and run_id-as-workflow-id are applied here: invoking
    twice with the same run_id returns the first run's result without
    re-executing (DBOS idempotency).
    """
    with SetWorkflowTimeout(WORKFLOW_TIMEOUT_SECONDS), SetWorkflowID(run_id):
        return pipeline_run(tenant_id, run_id, inbound)


# --- VT-3.3a: Twilio inbound webhook ingress pipeline ------------------------
#
# A separate workflow from pipeline_run (VT-3.1's LangGraph-substrate smoke
# path) — the ingress pipeline is ingress -> Pre-Filter Gate -> direct handler.
# pipeline_run is left untouched so VT-3.1's synthetic tests keep passing.


# Keys forbidden from any JSONB persisted into pipeline_runs.trigger_payload
# or pipeline_steps.input_envelope. ``body`` is the WhatsApp message text;
# the rest are defensive aliases. Centralised here so a future caller cannot
# bypass redaction by passing a body-bearing dict — VT-144 (PR #45) placed
# the pop at the caller (webhook_pipeline_run); this PR pushes it to the
# persistence boundary so NO write path to either sink can leak.
_REDACTED_KEYS_AT_REST = frozenset({"body", "message_body", "raw_text", "content"})


def _redact_for_persistence(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``payload`` with all
    ``_REDACTED_KEYS_AT_REST`` removed.

    Single source of truth for "what must never reach
    ``pipeline_runs.trigger_payload`` / ``pipeline_steps.input_envelope``".
    Shallow-copy is correct here — the persisted envelope is one level
    of keys; redaction operates on top-level keys only by design (per
    the VT-144 / VT-Privacy-Body brief, message-content fields live at
    the top level).
    """
    return {k: v for k, v in payload.items() if k not in _REDACTED_KEYS_AT_REST}


@DBOS.step()
def open_webhook_run(tenant_id: str, run_id: str, trigger_payload: dict) -> None:
    """Record the inbound run in pipeline_runs. Idempotent — a redelivered
    MessageSid maps to the same run_id. trigger_payload is phone-tokenised.

    Body-key redaction is applied at this persistence boundary (NOT by
    the caller) so no future caller can leak message content into
    ``trigger_payload``. The redacted dict is wrapped in ``Jsonb`` for
    the INSERT; the input dict is not mutated.
    """
    safe_payload = _redact_for_persistence(trigger_payload)
    # VT-309: record the run AND the L2 owner_message_received episodic event in
    # ONE txn (atomic per Cowork ruling 20260603T191000Z). LIVE dispatch path —
    # highest care: the payload carries ONLY derived/structural fields
    # (message_type + body LENGTH), NEVER the raw body (CL-390 / CL-330). The
    # body never enters the episodic row. Gated to real inbound messages (not
    # status-callbacks, not dupes); deterministic event_id → idempotent on
    # redelivery / DBOS step retry.
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO pipeline_runs "
            "(id, tenant_id, run_type, status, trigger_payload) "
            "VALUES (%s, %s, 'twilio_inbound', 'running', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, tenant_id, Jsonb(safe_payload)),
        )
        if trigger_payload.get("message_type") == "inbound_message" and not trigger_payload.get(
            "dupe_status"
        ):
            from orchestrator.knowledge.l2_types import L2EventType
            from orchestrator.knowledge.l2_writer import (
                deterministic_event_id,
                record_episodic_event,
            )

            record_episodic_event(
                tenant_id,
                L2EventType.OWNER_MESSAGE_RECEIVED,
                payload={
                    "message_type": "inbound_message",
                    "body_length": len(trigger_payload.get("body") or ""),
                    "has_media": bool(trigger_payload.get("num_media", 0)),
                    "run_id": run_id,
                },
                referenced_entity_type="run",
                referenced_entity_id=run_id,
                event_id=deterministic_event_id(
                    tenant_id, L2EventType.OWNER_MESSAGE_RECEIVED, run_id
                ),
                conn=conn,
            )


@DBOS.step()
def record_webhook_received(tenant_id: str, run_id: str, envelope: dict) -> None:
    """Write the webhook_received step_record (step_seq=0) to pipeline_steps.

    The envelope is phone-tokenised — no plaintext PII (Pillar 3 / Pillar 7).
    Body-key redaction is applied at this persistence boundary so no
    future caller can leak message content into ``input_envelope``.

    Idempotency is provided by the DBOS workflow-id boundary for COMPLETED
    steps. A crash between the SQL commit and DBOS recording the step causes
    re-execution on workflow resume — hence the ON CONFLICT (run_id, step_seq)
    DO NOTHING clause. Migration 014's UNIQUE (run_id, step_seq) constraint
    makes ON CONFLICT well-defined.
    """
    safe_envelope = _redact_for_persistence(envelope)
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, input_envelope, status) "
            "VALUES (%s, %s, 0, 'webhook_received', %s, 'completed') "
            "ON CONFLICT (run_id, step_seq) DO NOTHING",
            (run_id, tenant_id, Jsonb(safe_envelope)),
        )


@DBOS.step()
def close_webhook_run(tenant_id: str, run_id: str, status: str) -> None:
    """Mark the inbound run finished. Idempotent.

    tenant_id is required so the UPDATE runs under tenant_connection — the
    WHERE id = %s is scoped by the RLS USING clause, so without the GUC set
    the UPDATE silently affects 0 rows (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now() WHERE id = %s",
            (status, run_id),
        )


# Dispatch final_status values that mean the agent dispatch TERMINATED (failure/
# limit), vs 'completed' (success) and 'paused' (not terminal — resumes later).
_DISPATCH_TERMINATED_STATUSES = frozenset({"aborted_hard_limit", "escalated", "failed"})


@DBOS.step()
def record_dispatch_terminal_episodic(
    tenant_id: str, run_id: str, final_status: str, terminal_path: str | None
) -> None:
    """VT-309 — emit the L2 agent-dispatch lifecycle episodic event for a brain
    dispatch's terminal status.

    'completed' → agent_dispatch_completed; a terminated status → agent_dispatch_
    terminated; 'paused' (and anything unrecognised) → no emit (not a terminal
    decision — never guess). Best-effort: an emit failure must not fail the
    durable workflow. Safely at-least-once, NOT txn-atomic with the pipeline_runs
    status write — these are derived observability-lifecycle events (the run-status
    row is the source of truth); the DBOS step boundary + deterministic event_id
    make a retry a no-op (episodic_events UNIQUE(tenant_id, event_id)).
    """
    # VT-356: terminal_path is str | None (the terminated branch carries it raw), so the payload
    # value type is str | None — annotate it, else mypy widens to dict[str, str] from the first
    # branch and the 2nd branch's None-able entry is flagged.
    payload: dict[str, str | None]
    if final_status == "completed":
        event_type = "agent_dispatch_completed"
        payload = {"run_id": run_id, "outcome": terminal_path or final_status}
    elif final_status in _DISPATCH_TERMINATED_STATUSES:
        event_type = "agent_dispatch_terminated"
        payload = {"run_id": run_id, "reason": final_status, "terminal_path": terminal_path}
    else:
        return  # paused / unrecognised → not a terminal decision
    try:
        from orchestrator.knowledge.l2_writer import (
            deterministic_event_id,
            record_episodic_event,
        )

        record_episodic_event(
            tenant_id,
            event_type,
            payload=payload,
            referenced_entity_type="run",
            referenced_entity_id=run_id,
            event_id=deterministic_event_id(tenant_id, event_type, run_id),
        )
    except Exception:  # noqa: BLE001 — L2 projection must never fail the workflow
        logger.exception(
            "VT-309 dispatch-terminal L2 emit failed (tenant=%s run=%s status=%s)",
            tenant_id,
            run_id,
            final_status,
        )


# Twilio status-callback states (vs a plain inbound message).
_CALLBACK_STATES = {"delivered", "read", "failed", "undelivered"}


def build_webhook_event(fields: dict[str, Any], dupe_status: bool) -> WebhookEvent:
    """Construct a WebhookEvent from raw Twilio fields. Plain helper (no LLM)."""
    callback_state = fields.get("MessageStatus")
    is_callback = callback_state in _CALLBACK_STATES
    return WebhookEvent(
        body=str(fields.get("Body", "")),
        sender_phone=str(fields.get("From", "")),
        message_type="status_callback" if is_callback else "inbound_message",
        twilio_message_sid=fields.get("MessageSid"),
        status_callback_state=callback_state if is_callback else None,
        dupe_status=dupe_status,
        num_media=int(fields.get("NumMedia", 0) or 0),
        media_url_0=fields.get("MediaUrl0"),
    )


@DBOS.step()
def record_inbound_message_sid(tenant_id: str, message_sid: str) -> bool:
    """Record the MessageSid in the idempotency ledger — the FIRST workflow step.

    Returns True if newly inserted, False if already seen. C2 fix (CL-72): this
    runs inside the durable workflow boundary, so a half-completed ingress can
    never leave a row that makes the next attempt look like a duplicate.
    """
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s) ON CONFLICT (message_sid) DO NOTHING",
            (message_sid, tenant_id),
        )
        return cur.rowcount == 1


@DBOS.step()
def try_resume_pending_approval(tenant_id: str, body: str, message_sid: str | None) -> str | None:
    """VT-47 — if the tenant has a PAUSED run awaiting owner approval, treat
    this inbound message as the approval decision and resume that run.

    Returns the resolved decision verb ('approved'|'rejected'|'needs_changes')
    if this message was consumed as an approval reply, else None (the message
    is a normal inbound — fall through to pre_filter/dispatch).

    Pillar 7: an unclear reply (other / low-confidence) does NOT resolve the
    gate (resolve_decision_from_reply returns None) — the run stays paused and
    the message falls through. We never guess approval.

    Steps (all under the tenant GUC so RLS is real):
      1. Find the most-recent open pending_approvals for the tenant.
      2. Classify the reply (VT-49). None -> not consumed.
      3. Mark the row resolved (decision + status + resolved_at).
      4. Resume the paused LangGraph run with Command(resume={decision}).
      5. Drive the ORIGINAL paused run's pipeline_runs.status -> 'completed'.
    """
    from orchestrator.agent.approval_resume import (
        find_open_approval_for_tenant,
        mark_approval_resolved,
        resolve_decision_from_reply,
        resume_run,
    )

    with tenant_connection(tenant_id) as conn:
        approval = find_open_approval_for_tenant(conn, tenant_id)
    if approval is None:
        return None

    decision = resolve_decision_from_reply(body, tenant_id=tenant_id)
    if decision is None:
        # Unclear reply — leave the gate paused (Pillar 7: no guessing).
        return None

    # VT-309: resolve the approval + emit the L2 episodic decision ATOMICALLY
    # (one txn — the autocommit site the plan flagged; now wrapped per Cowork
    # ruling 20260603T191000Z). approved → campaign_approved, rejected →
    # campaign_rejected; needs_changes has no L2 milestone type → no emit.
    with tenant_connection(tenant_id) as conn, conn.transaction():
        resolved = mark_approval_resolved(
            conn, tenant_id, approval["id"], decision, owner_message_sid=message_sid
        )
        # VT-334: a 'defer' that only EXTENDS the window returns resolved=False — the run stays
        # paused (no L2 emit, no resume). The L2 + resume happen only on a real resolution
        # (incl. an exhausted defer, which resolves as a rejection).
        _l2_event = (
            {
                "approved": "campaign_approved",
                "rejected": "campaign_rejected",
                "defer": "campaign_rejected",  # an exhausted defer resolves as a rejection
            }.get(decision)
            if resolved
            else None
        )
        # Only campaign approvals map to an L2 milestone; other approval_types
        # (sensitive_data_access, …) have no campaign_* episodic type.
        if _l2_event is not None and approval.get("approval_type") == "campaign_send":
            from orchestrator.knowledge.l2_writer import (
                deterministic_event_id,
                record_episodic_event,
            )

            _campaign_id = approval.get("campaign_id")
            record_episodic_event(
                tenant_id,
                _l2_event,
                payload={
                    "campaign_id": _campaign_id,
                    "approval_id": str(approval["id"]),
                },
                referenced_entity_type="campaign" if _campaign_id else "approval",
                referenced_entity_id=_campaign_id or approval["id"],
                event_id=deterministic_event_id(tenant_id, _l2_event, approval["id"]),
                conn=conn,
            )

    # VT-334: a defer that only EXTENDED the window leaves the run PAUSED — do not resume or
    # close. The owner gets another 48h; the next reply re-enters here.
    if not resolved:
        logger.info(
            "approval-resume: deferred (window extended) tenant=%s approval=%s",
            tenant_id, approval["id"],
        )
        return decision

    # Resume the suspended graph (re-enters the interrupting node; the node's
    # arm_pause_request is a no-op now the row is resolved). Then close the
    # original paused run.
    paused_run_id = approval["run_id"]
    resume_run(paused_run_id, decision)
    close_webhook_run(tenant_id, paused_run_id, "completed")

    logger.info(
        "approval-resume: resolved tenant=%s approval=%s run=%s decision=%s",
        tenant_id,
        approval["id"],
        paused_run_id,
        decision,
    )
    return decision


@DBOS.workflow()
def webhook_pipeline_run(tenant_id: str, run_id: str, twilio_fields: dict) -> dict[str, Any]:
    """Durable inbound-webhook pipeline: dedup -> ingress -> Pre-Filter -> handler.

    Started by /api/orchestrator/twilio-ingress with a workflow_id derived from
    the Twilio MessageSid (DBOS exactly-once idempotency). Dedup detection and
    event construction happen inside this durable boundary (C2 fix, CL-72).
    """
    message_sid = str(twilio_fields.get("MessageSid", ""))
    newly_inserted = record_inbound_message_sid(tenant_id, message_sid)
    event = build_webhook_event(twilio_fields, dupe_status=not newly_inserted)
    state = new_subscriber_state(UUID(tenant_id), UUID(run_id))

    # Phone-tokenise before anything is persisted (Pillar 3 / Pillar 7).
    # Body-key redaction lives at the persistence boundary inside
    # ``open_webhook_run`` / ``record_webhook_received`` — see
    # ``_redact_for_persistence`` above. The caller no longer pops body
    # so future call sites cannot leak by forgetting to pop; centralised
    # at the writer per VT-Privacy-Writer-Side. The in-memory ``event``
    # keeps body intact for request-scoped readers (pre_filter, the
    # owner_inputs extraction writer when its SHIP GATE clears).
    tokenised = event.model_dump()
    if event.sender_phone:
        tokenised["sender_phone"] = hash_phone(event.sender_phone)

    open_webhook_run(tenant_id, run_id, tokenised)
    record_webhook_received(tenant_id, run_id, tokenised)

    # VT-146 — owner-input extraction seam. Reads body from the
    # request-scoped ``event`` (NOT from any persisted column; VT-144
    # stripped raw body from trigger_payload / input_envelope), routes
    # it to the classifier in ``orchestrator.owner_inputs`` (which owns
    # the LLM seam — Pillar 1 keeps runner.py deterministic; the LLM
    # call lives behind the writer's boundary), persists only the
    # derived intent / segment / occasion row to ``owner_inputs``.
    # ``run_extraction_for_event`` is best-effort internally —
    # classifier or write failure logs and returns None; the inbound
    # pipeline never breaks. No body leaves this function via
    # persistence; the body text crosses the wire to the classifier
    # only.
    #
    # Gated by ``OWNER_INPUTS_EXTRACTION_ENABLED`` (module-level
    # constant) — stays False until the vendor DPA + ZDR + the
    # privacy notice clear. See the constant's comment above.
    if OWNER_INPUTS_EXTRACTION_ENABLED:
        run_extraction_for_event(UUID(tenant_id), UUID(run_id), event)

    # VT-47 — owner-approval RESUME gate. If this tenant has a run PAUSED on
    # an owner-approval interrupt, an inbound owner message is the approval
    # decision: classify it (VT-49), resolve the pending_approvals row, and
    # resume the paused run via Command(resume=...). Status callbacks are not
    # decisions, so only inbound_message events are considered. When consumed,
    # THIS inbound run ends cleanly (the work was the resume); we do not also
    # route it through pre_filter/dispatch (that would double-handle the reply).
    if event.message_type == "inbound_message" and not event.dupe_status:
        resumed_decision = try_resume_pending_approval(
            tenant_id, event.body or "", event.twilio_message_sid
        )
        if resumed_decision is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "approval_resume",
                "handler": None,
                "decision": resumed_decision,
            }

    # VT-367 — onboarding-JOURNEY gate. While an onboarding journey is active (or a fresh tenant's
    # FIRST inbound, which lazy-starts it so the first message never reaches the cold brain), an
    # inbound owner message routes to the journey handler BEFORE pre_filter/dispatch. FAIL-OPEN:
    # maybe_handle_journey_reply swallows any error + returns None → the normal pipeline runs (owner
    # inbound is never blocked by a journey-check failure). Only inbound, non-dupe (idempotency is
    # double-guarded: the VT-149 message_sid UNIQUE seam above + handle_reply's last_message_sid).
    # Lazily imported so non-journey paths don't pay the import cost.
    if event.message_type == "inbound_message" and not event.dupe_status:
        from orchestrator.onboarding.journey import maybe_handle_journey_reply

        journey_result = maybe_handle_journey_reply(
            tenant_id, event.body or "", event.twilio_message_sid, event.sender_phone
        )
        if journey_result is not None:
            close_webhook_run(tenant_id, run_id, "completed")
            return {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "routed": "onboarding_journey",
                "handler": None,
                "journey_done": journey_result.get("done"),
            }

    result = pre_filter(event, state)
    handler_name: str | None = None
    # VT-356: `routed` is a local observability label (logged/returned), not the route-decision
    # type — widen to str so the VT-303 'consent_required' branch (below) is assignable.
    routed: str = result.kind
    final_status = "completed"
    if result.kind == "direct_handler":
        handler_name = result.handler_name
        HANDLERS[handler_name](event, state)
    elif result.kind == "brain":
        # VT-303 / CL-425 — owner_inputs consent gate on the brain transmit
        # (Option B). The brain transmits the owner's inbound body (may carry
        # customer PII) to Anthropic; owner_inputs is the lawful basis. Scope
        # the gate to real inbound messages — status-callback brain routes carry
        # no body, so there is nothing to gate. Fail-closed: FALSE/unknown →
        # NO transmit; send the conservative enable-prompt instead. The owner
        # turns it on via the enable keyword (data_inputs_enable_handler).
        if event.message_type == "inbound_message" and not _brain_owner_inputs_ok(tenant_id):
            handler_name = "consent_required_handler"
            routed = "consent_required"
            HANDLERS[handler_name](event, state)
        else:
            # VT-193: brain wired into supervisor graph via dispatch_brain.
            # Replaces the VT-3.4 placeholder (record_brain_pending + 'escalated'
            # final status) that the 2026-05-27 E2E surfaced. Imported lazily
            # so non-brain webhook paths don't pay the langchain/langgraph
            # import cost.
            from orchestrator.agent.dispatch import dispatch_brain

            dispatch_result = dispatch_brain(
                event=event,
                state=state,
                run_id=UUID(run_id),
                tenant_id=UUID(tenant_id),
            )
            final_status = dispatch_result.final_status
            # VT-309: L2 agent-dispatch lifecycle event (completed/terminated).
            # Brain path only — direct-handler/reject/consent runs are not agent
            # dispatches. Skips 'paused' (resolves later on resume).
            record_dispatch_terminal_episodic(
                tenant_id, run_id, final_status, dispatch_result.terminal_path
            )
            # VT-73 POST-FLIGHT isolation audit: service-role scan of this run's
            # pipeline_steps — assert no step was logged under another tenant
            # (catches a leak that escaped pre/in-flight). Best-effort detect+alert.
            from orchestrator.context_validator import audit_run_isolation

            audit_run_isolation(UUID(run_id), UUID(tenant_id))
    # result.kind == "reject" → observability-only; the run ends clean (completed).

    close_webhook_run(tenant_id, run_id, final_status)

    # VT-88 SupportBot: on an UNRESOLVED terminal the owner must get SOMETHING (not silence)
    # — an ack; the 2nd+ unresolved run in 24h also escalates to Fazal. Runs AFTER the status
    # is persisted (the deterministic counter includes this run). Best-effort — the fallback
    # must never break the durable run.
    try:
        from orchestrator.owner_surface.support_bot import maybe_escalate_support

        maybe_escalate_support(
            tenant_id=tenant_id, run_id=run_id, event=event, final_status=final_status
        )
    except Exception:  # noqa: BLE001 — the fallback must never break the workflow
        logger.exception(
            "VT-88 support escalation hook failed (tenant=%s run=%s)", tenant_id, run_id
        )

    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "routed": routed,
        "handler": handler_name,
    }
