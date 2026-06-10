"""VT-369 Gap-5 — Pillar-7 approval glue for the agent customer-messaging surface.

Two halves, both riding the EXISTING approval machinery (never a fork of it):

ARM  — ``arm_agent_send_approval`` arms a ``pending_approvals`` row of type
       ``agent_customer_send`` through ``arm_pause_request`` (the single arming
       path, which owns the VT-369 §4.1/F5 per-tenant queue serialization + the
       migration-128 one-open-per-tenant race backstop). Refusal/error RAISES the
       typed ``ApprovalArmRefused`` — the executor's contract is
       exception-equals-defer-to-next-sweep (it cancels the batch fail-closed; an
       unarmed batch must never sit armable).

RESOLVE — ``apply_agent_decision`` translates the owner's resolution verb into
       the ``agent_draft_batches`` state machine, called from
       ``approval_resume.mark_approval_resolved`` (the single resolution choke
       point: owner-reply path AND the 30-min timeout sweep) on the SAME
       connection/transaction as the resolve (plan §4.3):

         approved      -> batch 'approved'   (customer_send's stack takes over)
         needs_changes -> batch 'edit_requested' + owner_feedback stored +
                          edit_cycles+1 — ONE regeneration max; a SECOND
                          needs_changes is terminal 'rejected'. The edit_cycles
                          read is in-transaction under FOR UPDATE (Critic-2 1g:
                          never from workflow memory).
         rejected      -> batch 'rejected'
         timeout/defer -> batch 'cancelled'  (exhausted defer resolves as a
                          rejection upstream; the batch outcome is a cancel)

BINDING no-PII rule (plan §3d-1): the ``pending_approvals`` row carries
``draft_batch_id`` + counts ONLY — summary/details NEVER contain customer names,
phones, or draft text. The ``sample_message`` the owner reviews is rendered at
arm time from an RLS read of ``agent_drafts`` and goes into the WhatsApp
template send ONLY (``team_agent_draft_approval`` {{3}}), never into the row.

CL-390: logs carry tenant/batch/approval ids + counts + statuses only — never
``owner_feedback``, never ``sample_message``, never draft params.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from orchestrator.db.wrappers import PendingApprovalsWrapper

logger = logging.getLogger(__name__)

# VT-163 registry name for the L2 agent-draft approval ask (owner-targeted;
# fail-closed 'template_not_yet_approved' until the F1 Meta SIDs land).
AGENT_APPROVAL_TEMPLATE_NAME = "team_agent_draft_approval"

# Plan §4.3: ONE regeneration max — a batch that has already burned its edit
# cycle resolves a second needs_changes as terminal 'rejected'.
MAX_EDIT_CYCLES = 1

# The ONLY details keys an agent approval row may carry (the no-PII CI pin).
ALLOWED_DETAILS_KEYS = frozenset({"draft_batch_id", "draft_count"})

# Batch states an arm may transition INTO 'awaiting_approval' from. The
# sales_recovery executor persists batches as 'awaiting_approval' already
# (atomic with the drafts), so this is the re-arm path ('edit_requested' after a
# regeneration) + a belt-and-braces for 'drafting' producers.
_ARMABLE_FROM = ("drafting", "edit_requested")

# Batch states a resolution may act on. Anything else (sent/cancelled/halted/…)
# is stale — the resolution is a no-op on the batch (logged, never an error).
_RESOLVABLE_FROM = ("drafting", "awaiting_approval")


class ApprovalArmRefused(RuntimeError):
    """Typed refusal from ``arm_agent_send_approval`` — the agent caller's
    defer-to-next-sweep signal (plan §4.1). Carries the refusal code so the
    executor's counters can distinguish queue-busy from template-send failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class AgentBatchDecision(BaseModel):
    """Outcome of applying an owner decision to an agent draft batch."""

    model_config = ConfigDict(frozen=True)

    batch_id: str
    batch_status: str
    edit_cycles: int
    # True exactly when this needs_changes was ACCEPTED for the one allowed
    # regeneration (the workflow may redraft once, then re-arm).
    regeneration_requested: bool = False


# ---------------------------------------------------------------------------
# ARM
# ---------------------------------------------------------------------------


def _batch_draft_count(conn: Any, tenant_id: str, batch_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM agent_drafts "
        "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted'",
        (tenant_id, batch_id),
    ).fetchone()
    if not row:
        return 0
    return int(row["n"] if isinstance(row, dict) else row[0])


def _render_sample_message(conn: Any, tenant_id: str, batch_id: str) -> str:
    """Param-level preview of the batch's FIRST draft, rendered from an RLS read
    of ``agent_drafts`` (plan §3d-1). May contain a customer display name — it is
    sent to the OWNER inside the WhatsApp approval template ONLY and is never
    persisted in ``pending_approvals`` (the caller owns that invariant)."""
    row = conn.execute(
        "SELECT template_name, params FROM agent_drafts "
        "WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted' "
        "ORDER BY created_at ASC LIMIT 1",
        (tenant_id, batch_id),
    ).fetchone()
    if not row:
        return ""
    template_name = row["template_name"] if isinstance(row, dict) else row[0]
    params = (row["params"] if isinstance(row, dict) else row[1]) or {}
    rendered = ", ".join(f"{k}: {v}" for k, v in sorted(params.items()))
    return f"[{template_name}] {rendered}"


def _owner_display_name(conn: Any, tenant_id: str) -> str:
    row = conn.execute(
        "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
    ).fetchone()
    if not row:
        return "Owner"
    name = row["business_name"] if isinstance(row, dict) else row[0]
    return str(name) if name else "Owner"


def arm_agent_send_approval(
    tenant_id: UUID | str,
    run_id: UUID | str,
    batch_id: UUID | str,
    counts: dict[str, Any] | None = None,
    *,
    draft_count: int | None = None,
    sample_message: str | None = None,
    conn: Any = None,
    send_fn: Any | None = None,
    dry_run: bool = False,
) -> Any:
    """Arm the Pillar-7 gate for an agent draft batch. Returns the ARMED
    ``PauseRequestResult``; raises ``ApprovalArmRefused`` on refusal/error (the
    executor cancels the batch fail-closed and the next sweep retries —
    exception-equals-defer is its contract).

    ``counts`` is the executor's positional counters dict (``{'drafted': N}``);
    ``draft_count`` overrides it; when neither is given the count is read from
    ``agent_drafts``. ``sample_message`` defaults to an arm-time RLS render of
    the batch's first draft. ``conn`` (optional) composes the arm + the batch
    flip atomically on a caller-owned tenant_connection.

    The persisted row is PII-free BY CONSTRUCTION: summary/details are built
    here from ids + counts only (``ALLOWED_DETAILS_KEYS``); ``sample_message``
    rides the template send exclusively.
    """
    from contextlib import contextmanager

    from orchestrator.agent.tools.request_owner_approval import (
        RequestOwnerApprovalInput,
        arm_pause_request,
    )
    from orchestrator.db import tenant_connection

    tid, rid, bid = str(tenant_id), str(run_id), str(batch_id)

    @contextmanager
    def _reuse(_tenant: UUID | str):  # caller-owned conn: do not close it
        yield conn

    conn_factory = _reuse if conn is not None else tenant_connection

    # Arm-time RLS reads (count / sample / owner name) on a tenant-scoped conn.
    with conn_factory(tid) as c:
        n = int(draft_count) if draft_count is not None else int(
            (counts or {}).get("drafted") or _batch_draft_count(c, tid, bid)
        )
        if n < 1:
            raise ApprovalArmRefused(
                "empty_batch", f"batch {bid} has no drafted rows — nothing to approve"
            )
        sample = (
            sample_message
            if sample_message is not None
            else _render_sample_message(c, tid, bid)
        )
        owner_name = _owner_display_name(c, tid)

    payload = RequestOwnerApprovalInput(
        tenant_id=UUID(tid),
        run_id=UUID(rid),
        approval_type="agent_customer_send",
        # No customer PII: batch id + count ONLY (plan §3d-1, the binding rule).
        summary=f"Agent drafted {n} customer message(s) — batch {bid}. Approve to send?",
        details={"draft_batch_id": bid, "draft_count": n},
        # sample_message goes into the WhatsApp send, NEVER the row.
        template_params={
            "owner_name": owner_name,
            "draft_count": str(n),
            "sample_message": sample,
        },
        draft_batch_id=UUID(bid),
        template_name=AGENT_APPROVAL_TEMPLATE_NAME,
    )

    result = arm_pause_request(
        payload,
        conn_factory=conn_factory if conn is not None else None,
        send_fn=send_fn,
        dry_run=dry_run,
    )
    if result.status != "armed":
        code = result.error.code if result.error else result.status
        message = result.error.message if result.error else "arm refused"
        raise ApprovalArmRefused(code, message)

    # Flip a not-yet-awaiting batch into 'awaiting_approval' (idempotent: the
    # executor already persists batches as awaiting — 0 rows updated is fine).
    with conn_factory(tid) as c:
        c.execute(
            "UPDATE agent_draft_batches SET status = 'awaiting_approval', "
            "updated_at = now() WHERE tenant_id = %s AND id = %s AND status = ANY(%s)",
            (tid, bid, list(_ARMABLE_FROM)),
        )

    logger.info(
        "approval_glue: armed agent_customer_send tenant=%s batch=%s approval=%s drafts=%d",
        tid, bid, result.approval_id, n,
    )
    return result


# ---------------------------------------------------------------------------
# RESOLVE
# ---------------------------------------------------------------------------


def apply_agent_decision(
    conn: Any,
    tenant_id: UUID | str,
    approval_row: dict[str, Any],
    decision: str,
    *,
    owner_feedback: str | None = None,
) -> AgentBatchDecision | None:
    """Apply a RESOLVED owner decision to the linked agent draft batch (plan
    §4.3), on the caller's connection — atomic with ``mark_resolved`` when the
    caller wraps both in one transaction (the runner does).

    No-op (returns None) for: non-``agent_customer_send`` rows, rows with no
    ``draft_batch_id`` (SET NULL'd), unknown decisions, and batches no longer in
    a resolvable state. ``owner_feedback`` is persisted on the batch row for a
    needs_changes (RLS-protected; NEVER logged — CL-390).
    """
    approval_id = approval_row.get("id")
    if approval_id is None:
        return None

    # Re-read the durable row (type + batch link) — never trust caller memory.
    row = PendingApprovalsWrapper().find_by_id(tenant_id, approval_id, conn=conn)
    if row is None or row.get("approval_type") != "agent_customer_send":
        return None
    batch_id = row.get("draft_batch_id")
    if batch_id is None:
        return None
    tid, bid = str(tenant_id), str(batch_id)
    agent_row = conn.execute(
        "SELECT agent FROM agent_draft_batches WHERE tenant_id = %s AND id = %s", (tid, bid)
    ).fetchone()
    agent = (agent_row["agent"] if isinstance(agent_row, dict) else agent_row[0]) if agent_row else None

    if decision == "approved":
        updated = conn.execute(
            "UPDATE agent_draft_batches SET status = 'approved', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = ANY(%s) "
            "RETURNING edit_cycles",
            (tid, bid, list(_RESOLVABLE_FROM)),
        ).fetchone()
        # VT-369 PR-2: the clean-approval streak is counted IN THIS resolution txn, with
        # edit_cycles read from the UPDATE's RETURNING row — never workflow memory (§5.2).
        if updated is not None and agent:
            from orchestrator.agents.autonomy import record_approval_outcome

            cycles = int(updated["edit_cycles"] if isinstance(updated, dict) else updated[0])
            record_approval_outcome(tid, agent, clean=(cycles == 0), conn=conn)
        return _decided(tid, bid, "approved", updated)

    if decision == "needs_changes":
        # In-txn edit_cycles read under FOR UPDATE (Critic-2 1g) — the streak /
        # regeneration arithmetic never rides workflow memory.
        locked = conn.execute(
            "SELECT edit_cycles FROM agent_draft_batches "
            "WHERE tenant_id = %s AND id = %s AND status = ANY(%s) FOR UPDATE",
            (tid, bid, list(_RESOLVABLE_FROM)),
        ).fetchone()
        if locked is None:
            logger.info(
                "approval_glue: needs_changes on a non-resolvable batch tenant=%s "
                "batch=%s — no-op", tid, bid,
            )
            return None
        edit_cycles = int(
            locked["edit_cycles"] if isinstance(locked, dict) else locked[0]
        )
        if edit_cycles >= MAX_EDIT_CYCLES:
            # Second needs_changes — terminal (ONE regeneration max, plan §4.3).
            updated = conn.execute(
                "UPDATE agent_draft_batches SET status = 'rejected', updated_at = now() "
                "WHERE tenant_id = %s AND id = %s RETURNING edit_cycles",
                (tid, bid),
            ).fetchone()
            if updated is not None and agent:
                from orchestrator.agents.autonomy import record_regression_event

                record_regression_event(tid, agent, "reject", conn=conn)
            return _decided(tid, bid, "rejected", updated)
        updated = conn.execute(
            "UPDATE agent_draft_batches SET status = 'edit_requested', "
            "owner_feedback = %s, edit_cycles = edit_cycles + 1, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s RETURNING edit_cycles",
            (owner_feedback, tid, bid),
        ).fetchone()
        if updated is not None and agent:
            from orchestrator.agents.autonomy import record_regression_event

            record_regression_event(tid, agent, "edit", conn=conn)
        out = _decided(tid, bid, "edit_requested", updated)
        return (
            out.model_copy(update={"regeneration_requested": True})
            if out is not None
            else None
        )

    if decision == "rejected":
        updated = conn.execute(
            "UPDATE agent_draft_batches SET status = 'rejected', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = ANY(%s) "
            "RETURNING edit_cycles",
            (tid, bid, list(_RESOLVABLE_FROM)),
        ).fetchone()
        if updated is not None and agent:
            from orchestrator.agents.autonomy import record_regression_event

            record_regression_event(tid, agent, "reject", conn=conn)
        return _decided(tid, bid, "rejected", updated)

    if decision in ("timeout", "defer"):
        # Timeout sweep / exhausted defer: no send, batch cancelled (plan §4.3).
        updated = conn.execute(
            "UPDATE agent_draft_batches SET status = 'cancelled', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = ANY(%s) "
            "RETURNING edit_cycles",
            (tid, bid, list(_RESOLVABLE_FROM)),
        ).fetchone()
        if updated is not None and agent:
            from orchestrator.agents.autonomy import record_regression_event

            record_regression_event(tid, agent, "reject", conn=conn)
        return _decided(tid, bid, "cancelled", updated)

    return None  # unknown verb — never guess (Pillar 7)


def _decided(
    tid: str, bid: str, status: str, updated: Any
) -> AgentBatchDecision | None:
    """Build the decision result from the UPDATE's RETURNING row (None = the
    batch was not in a resolvable state — stale resolution, logged no-op)."""
    if updated is None:
        logger.info(
            "approval_glue: decision on a non-resolvable batch tenant=%s batch=%s "
            "wanted=%s — no-op", tid, bid, status,
        )
        return None
    edit_cycles = int(
        updated["edit_cycles"] if isinstance(updated, dict) else updated[0]
    )
    logger.info(
        "approval_glue: batch decided tenant=%s batch=%s status=%s edit_cycles=%d",
        tid, bid, status, edit_cycles,
    )
    return AgentBatchDecision(
        batch_id=bid, batch_status=status, edit_cycles=edit_cycles
    )


__all__ = [
    "AGENT_APPROVAL_TEMPLATE_NAME",
    "ALLOWED_DETAILS_KEYS",
    "MAX_EDIT_CYCLES",
    "AgentBatchDecision",
    "ApprovalArmRefused",
    "apply_agent_decision",
    "arm_agent_send_approval",
]
