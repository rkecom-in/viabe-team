"""VT-369 Gap-5 PR-2 — the per-(tenant, agent) autonomy L2/L3 state machine (plan §5).

PR-2 ships the SUBSTRATE with ZERO auto-send behavior: the state table (migration 129), the
clean-approval streak counted in the approval-resolution transaction, the regression rules, the
freeze/revoke kill switches (which ATOMICALLY cancel in-flight batches — the binding rule: a kill
switch never leaves armed batches ticking), the always-confirm floor predicate, and the Gap-6 VTR
override seam. L3 GRANTING + auto-send arrive in PR-3; ``grant_l3`` exists here (with its in-txn
revalidation) so PR-3 wires a proposal flow onto an already-tested primitive.

Rules (plan §5.2/5.4, binding):
- CLEAN approval = decision='approved' via owner reply, batch ``edit_cycles == 0``, not timeout,
  not defer-exhausted. Counted in the SAME transaction as the resolution, with ``edit_cycles``
  read IN-TXN from the batch row — never from workflow memory.
- A regression resets the streak; at L3 it REVOKES (one-way per incident — re-earning = a fresh
  20-clean streak + a fresh owner opt-in); opt-out spikes / complaints / send-failures / the owner
  kill keyword also FREEZE. Every revoke/freeze cancels open batches atomically.
- Missing row == L2 (the default; nothing to read is nothing earned).

Owner notifications on revoke are PR-3 (templates); PR-2 emits observability events only —
zero owner-visible behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

L3_CLEAN_STREAK_THRESHOLD = 20  # plan §5.3 (F3 confirms)
L3_AUTO_MAX_BATCH = 20          # the bulk floor (§5.5)
L3_PROPOSAL_COOLDOWN_DAYS = 30
# VT-384 gate-bounce F4: the consecutive_silent_l3_notices counter is KEPT (mig-129 column +
# the wake-leg bump in l3_hold) as pure OBSERVABILITY + a VT-385 design input. The auto-demote
# THRESHOLD path was DROPPED — auto-demoting an opted-in owner for using Act mode exactly as
# designed fights CL-438 (the owner explicitly opted in; silence is not a regression). No
# threshold constant, no owner_disengaged-on-silence regression. VT-385 owns any disengagement
# design that uses this counter.

RegressionKind = Literal[
    "edit", "reject", "optout_spike", "complaint", "owner_cancel",
    "send_failure", "owner_disengaged", "owner_keyword",
]
# Kinds that ALSO freeze (the kill-switch class) — at either level.
_FREEZING_KINDS = frozenset({"optout_spike", "complaint", "send_failure", "owner_keyword"})

# Batches a freeze/revoke must kill: everything non-terminal. A frozen agent has NO live work —
# including awaiting_approval rows (an owner "yes" after the freeze must find nothing to approve).
_OPEN_BATCH_STATUSES = ("drafting", "awaiting_approval", "approved", "auto_send_pending", "sending")


@dataclass(frozen=True, slots=True)
class AutonomyState:
    tenant_id: UUID
    agent: str
    level: str
    clean_approval_streak: int
    lifetime_approvals: int
    lifetime_rejections: int
    frozen: bool
    l3_granted_at: Any = None
    l3_grant_approval_id: str | None = None
    # VT-610 — the FORCED provenance pair, independent of the earned pair above (both nullable; a
    # forced-only tenant shows l3_force_granted_at set + l3_granted_at NULL).
    l3_force_granted_at: Any = None
    l3_force_granted_by_vtr: str | None = None
    last_regression_kind: str | None = None
    # VT-384: the owner-disengagement counter (mig-129) — surfaced so callers can read it back
    # through get_autonomy (the silent-notice acceptance leg) without a second raw query.
    consecutive_silent_l3_notices: int = 0


def _row_to_state(tenant_id: UUID, agent: str, row: Any) -> AutonomyState:
    if row is None:
        return AutonomyState(tenant_id, agent, "L2", 0, 0, 0, False)
    g = dict(row) if isinstance(row, dict) else None
    if g is None:
        cols = ("level", "clean_approval_streak", "lifetime_approvals", "lifetime_rejections",
                "frozen", "l3_granted_at", "l3_grant_approval_id",
                "l3_force_granted_at", "l3_force_granted_by_vtr",
                "last_regression_kind", "consecutive_silent_l3_notices")
        g = dict(zip(cols, row, strict=False))
    return AutonomyState(
        tenant_id=tenant_id, agent=agent, level=g["level"],
        clean_approval_streak=int(g["clean_approval_streak"]),
        lifetime_approvals=int(g["lifetime_approvals"]),
        lifetime_rejections=int(g["lifetime_rejections"]),
        frozen=bool(g["frozen"]),
        l3_granted_at=g.get("l3_granted_at"),
        l3_grant_approval_id=str(g["l3_grant_approval_id"]) if g.get("l3_grant_approval_id") else None,
        l3_force_granted_at=g.get("l3_force_granted_at"),
        l3_force_granted_by_vtr=g.get("l3_force_granted_by_vtr"),
        last_regression_kind=g.get("last_regression_kind"),
        consecutive_silent_l3_notices=int(g.get("consecutive_silent_l3_notices") or 0),
    )


_SELECT = (
    "SELECT level, clean_approval_streak, lifetime_approvals, lifetime_rejections, frozen, "
    "l3_granted_at, l3_grant_approval_id, l3_force_granted_at, l3_force_granted_by_vtr, "
    "last_regression_kind, consecutive_silent_l3_notices "
    "FROM tenant_agent_autonomy WHERE tenant_id = %s AND agent = %s"
)


def get_autonomy(tenant_id: UUID | str, agent: str, *, conn: Any = None) -> AutonomyState:
    """Current state; a MISSING row is L2 with zero counters (the default)."""
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    if conn is not None:
        row = conn.execute(_SELECT, (str(tid), agent)).fetchone()
    else:
        with tenant_connection(tid) as c:
            row = c.execute(_SELECT, (str(tid), agent)).fetchone()
    return _row_to_state(tid, agent, row)


def _ensure_row(conn: Any, tid: str, agent: str) -> None:
    conn.execute(
        "INSERT INTO tenant_agent_autonomy (tenant_id, agent) VALUES (%s, %s) "
        "ON CONFLICT (tenant_id, agent) DO NOTHING",
        (tid, agent),
    )


def record_approval_outcome(
    tenant_id: UUID | str, agent: str, *, clean: bool, conn: Any
) -> AutonomyState:
    """Count an APPROVED batch resolution. MUST run on the resolution transaction's ``conn``
    (the same-txn discipline). ``clean`` per §5.2 — the CALLER derives it from an in-txn read of
    ``agent_draft_batches.edit_cycles`` (approval_glue does this), never from memory. A non-clean
    approval (owner edited first) counts as an approval but RESETS the streak (the 'edit'
    regression is recorded by the caller via record_regression_event)."""
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    if clean:
        conn.execute(
            "UPDATE tenant_agent_autonomy SET clean_approval_streak = clean_approval_streak + 1, "
            "lifetime_approvals = lifetime_approvals + 1, updated_at = now() "
            "WHERE tenant_id = %s AND agent = %s",
            (tid, agent),
        )
    else:
        conn.execute(
            "UPDATE tenant_agent_autonomy SET clean_approval_streak = 0, "
            "lifetime_approvals = lifetime_approvals + 1, updated_at = now() "
            "WHERE tenant_id = %s AND agent = %s",
            (tid, agent),
        )
    return get_autonomy(tenant_id, agent, conn=conn)


def cancel_open_batches(tenant_id: UUID | str, agent: str, *, reason: str, conn: Any) -> int:
    """Kill every non-terminal batch for (tenant, agent): batches → 'cancelled', their non-terminal
    drafts → 'halted'. Runs on the CALLER's conn so a revoke/freeze is atomic with it (the binding
    rule). Returns the number of batches cancelled."""
    tid = str(tenant_id)
    rows = conn.execute(
        "UPDATE agent_draft_batches SET status = 'cancelled', updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s AND status = ANY(%s) RETURNING id",
        (tid, agent, list(_OPEN_BATCH_STATUSES)),
    ).fetchall()
    batch_ids = [str(r["id"] if isinstance(r, dict) else r[0]) for r in rows]
    if batch_ids:
        conn.execute(
            "UPDATE agent_drafts SET status = 'halted', skip_reason = %s, updated_at = now() "
            "WHERE tenant_id = %s AND batch_id = ANY(%s::uuid[]) AND status = 'drafted'",
            (f"halted_{reason}", tid, batch_ids),
        )
        # VT-382 (CL-437.3): cancelled batches + halted drafts are terminal — redact
        # owner_feedback + halted params in the SAME revoke/freeze txn (no audit rows:
        # nothing was sent).
        from orchestrator.agents.outbox_redaction import redact_batch_close

        redact_batch_close(conn, tid, batch_ids)
    return len(batch_ids)


def record_regression_event(
    tenant_id: UUID | str, agent: str, kind: RegressionKind, *, conn: Any, detail: str | None = None
) -> AutonomyState:
    """Apply the §5.4 regression table on the caller's conn (same-txn discipline):
    streak → 0 always; lifetime_rejections +1 on 'reject'; FREEZE on the kill-switch kinds;
    at L3 → REVOKE to L2 (one-way per incident). Every revoke/freeze cancels open batches
    atomically. Emits an observability event (best-effort).

    VT-384 gate-bounce F5: ``detail`` carries the PRECISE regression reason (e.g.
    'owner_engaged' vs 'no_delivery' for a demote — both record the SAME ``owner_disengaged``
    kind, so the kind alone loses the distinction). It rides the ``agent_autonomy_regressed``
    observability event's ``detail`` field so the two demote causes are separable downstream
    without a kind-taxonomy change (that cleanup is VT-385's design input, per the ruling)."""
    from orchestrator.observability.tm_audit import emit_tm_audit
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    state = get_autonomy(tenant_id, agent, conn=conn)
    freezes = kind in _FREEZING_KINDS
    revokes = state.level == "L3" and kind != "owner_disengaged"
    conn.execute(
        "UPDATE tenant_agent_autonomy SET clean_approval_streak = 0, "
        "lifetime_rejections = lifetime_rejections + CASE WHEN %s THEN 1 ELSE 0 END, "
        "last_regression_at = now(), last_regression_kind = %s, "
        "frozen = frozen OR %s, "
        "level = CASE WHEN %s THEN 'L2' ELSE level END, "
        "l3_revoked_at = CASE WHEN %s THEN now() ELSE l3_revoked_at END, "
        "revoke_reason = CASE WHEN %s THEN %s ELSE revoke_reason END, "
        "updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s",
        (kind == "reject", kind, freezes, revokes, revokes, revokes, kind, tid, agent),
    )
    if freezes or revokes:
        cancelled = cancel_open_batches(tenant_id, agent, reason=kind, conn=conn)
    else:
        cancelled = 0
    emit_tm_audit(
        event_layer="does",
        event_kind="autonomy_change",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=None,
        action={
            "agent": agent,
            "kind": kind,
            "revoked": revokes,
            "frozen": freezes,
            "batches_cancelled": cancelled,
        },
        summary=f"autonomy regression: agent={agent} kind={kind}",
        conn=conn,
    )
    _emit(tid, "agent_autonomy_regressed", {"agent": agent, "kind": kind, "detail": detail,
                                            "revoked": revokes, "frozen": freezes,
                                            "batches_cancelled": cancelled})
    return get_autonomy(tenant_id, agent, conn=conn)


def l3_proposal_eligible(state: AutonomyState) -> bool:
    """The §5.3 eligibility predicate (the PROPOSAL flow itself is PR-3): a 20-clean streak, L2,
    not frozen. (The no-regression-in-30d and proposal-cooldown checks ride the PR-3 arming path,
    which reads the timestamps; this predicate is the substrate gate.)"""
    return state.level == "L2" and not state.frozen and (
        state.clean_approval_streak >= L3_CLEAN_STREAK_THRESHOLD
    )


def grant_l3(
    tenant_id: UUID | str, agent: str, approval_id: UUID | str, *, conn: Any
) -> AutonomyState:
    """Grant L3 — ONLY from an explicit owner opt-in (the autonomy_upgrade approval row id is the
    durable consent evidence, C3). REVALIDATES IN-TXN (plan §5.3): streak still at threshold, not
    frozen, still L2; stale → no-op (the caller notifies). PR-3 wires the proposal/approval flow."""
    from orchestrator.observability.tm_audit import emit_tm_audit
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    row = conn.execute(
        "UPDATE tenant_agent_autonomy SET level = 'L3', l3_granted_at = now(), "
        "l3_grant_approval_id = %s, consecutive_silent_l3_notices = 0, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s AND level = 'L2' AND frozen = false "
        "AND clean_approval_streak >= %s RETURNING level",
        (str(approval_id), tid, agent, L3_CLEAN_STREAK_THRESHOLD),
    ).fetchone()
    if row is None:
        logger.warning("grant_l3: stale grant no-op tenant=%s agent=%s", tid, agent)
    else:
        emit_tm_audit(
            event_layer="does",
            event_kind="autonomy_change",
            actor="team_manager",
            tenant_id=tenant_id,
            run_id=None,
            action={"agent": agent, "approval_id": str(approval_id), "new_level": "L3"},
            summary=f"autonomy granted L3: agent={agent}",
            conn=conn,
        )
        _emit(tid, "agent_autonomy_granted", {"agent": agent, "approval_id": str(approval_id)})
    return get_autonomy(tenant_id, agent, conn=conn)


def force_l3(
    tenant_id: UUID | str, agent: str, *, vtr_id: str, reason: str, conn: Any
) -> AutonomyState:
    """VT-610 — the VTR force_l3 override. Grants L3 LEVEL ONLY, bypassing ONLY the two things
    ``grant_l3`` normally requires: the earning threshold (``clean_approval_streak``) and owner
    opt-in (there is no ``approval_id`` here — ``vtr_id`` + a scrubbed ``reason`` are the
    provenance instead, mirroring ``vtr_autonomy_override``'s existing shape for freeze/revoke).

    Mirrors ``grant_l3``'s in-txn UPDATE...WHERE...RETURNING shape, but the WHERE clause DROPS the
    streak check (the bypass) and keeps ``frozen = false`` (NOT bypassed — a VTR must unfreeze
    first; forcing trust onto a live-frozen agent is a confused operator action this refuses, same
    as a stale ``grant_l3`` no-ops). Idempotent regardless of CURRENT level (a SET, not an
    earn-transition): forcing an already-L3 agent (earned or forced) just re-stamps the forced
    provenance — never an error, never a double-audit-of-nothing.

    NO batch cancellation (unlike demote/freeze/revoke_l3) — force_l3 only ever WIDENS trust, so
    there is nothing in-flight that needs killing; a batch already awaiting the owner's approval
    stays exactly as it was.

    Grants NOTHING beyond the ``level`` column: policy (business_policy.assert_within_policy),
    per-recipient consent/opt-out/complaint/caps (customer_send.agent_send_draft's gate stack),
    Gate-0 activation (onboarding_gate.is_agent_eligible), the always-confirm floor
    (is_always_confirm, above — re-derived per batch, never reads ``level`` at all), and the
    business-impact gates (business_impact_choke + the SEPARATE ``tenant_business_autonomy``
    table this function never touches) are all unconditional and untouched. A FUTURE regression
    freezes/revokes a forced-L3 agent through the EXACT SAME ``record_regression_event`` path as
    an earned one — force grants level, never immunity to a regression.

    ``emit_tm_audit`` runs on the caller's ``conn`` (fail-closed emit-or-rollback, mirroring every
    sibling here): an audit-insert failure raises and rolls back the level change with it — a
    force can never land without its audit trail."""
    from orchestrator.observability.tm_audit import emit_tm_audit
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    row = conn.execute(
        "UPDATE tenant_agent_autonomy SET level = 'L3', l3_force_granted_at = now(), "
        "l3_force_granted_by_vtr = %s, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s AND frozen = false RETURNING level",
        (vtr_id, tid, agent),
    ).fetchone()
    if row is None:
        logger.warning(
            "force_l3: refused (frozen) tenant=%s agent=%s vtr=%s", tid, agent, vtr_id
        )
    else:
        emit_tm_audit(
            event_layer="does",
            event_kind="autonomy_change",
            actor="team_manager",
            tenant_id=tenant_id,
            run_id=None,
            action={"agent": agent, "vtr_id": vtr_id, "reason": reason, "new_level": "L3",
                    "forced": True},
            summary=f"autonomy FORCE-granted L3 by VTR: agent={agent}",
            conn=conn,
        )
        _emit(tid, "agent_autonomy_force_granted", {"agent": agent, "vtr_id": vtr_id})
    return get_autonomy(tenant_id, agent, conn=conn)


def kill_autonomy_by_keyword(tenant_id: UUID | str, *, conn: Any) -> dict[str, int]:
    """VT-384 §B2.3 — the owner KILL keyword (an autonomy-specific "stop automatic sending", NOT a
    full DPDP opt-out). For EVERY owning agent of the tenant, record the ``owner_keyword``
    regression on the caller's conn (same-txn): that FREEZES the agent and ATOMICALLY cancels its
    in-flight holds + batches — ``_OPEN_BATCH_STATUSES`` includes ``auto_send_pending`` and
    ``sending``, so an L3 hold parked on its delivery anchor is cancelled the instant the keyword
    lands (the original race requirement; a window-expiry send can never fire over the objection).

    Owner-level by design: the offer/kill is owner-facing, so the keyword freezes the whole
    workspace's agent autonomy — not just one agent. Returns {agent: batches_cancelled_or_0}.
    Idempotent: a kill on an already-L2/frozen agent still records the regression (streak reset)
    and cancels any residual open batch."""
    from orchestrator.business_plan.store import OWNING_AGENTS

    out: dict[str, int] = {}
    for agent in sorted(OWNING_AGENTS - {"unassigned"}):
        before = get_autonomy(tenant_id, agent, conn=conn)
        record_regression_event(tenant_id, agent, "owner_keyword", conn=conn)
        # record_regression_event cancels open batches internally; surface a per-agent marker so the
        # handler can report a non-PII summary (count of agents touched).
        out[agent] = 1 if before.level == "L3" or before.frozen is False else 0
    return out


def offer_on_cooldown(
    tenant_id: UUID | str, *, cooldown_days: int = L3_PROPOSAL_COOLDOWN_DAYS, conn: Any
) -> bool:
    """VT-384 §B2.1 cooldown — True if an ``autonomy_upgrade`` offer was already armed for this
    tenant within ``cooldown_days`` (open OR resolved). The approval row IS the offer record, so no
    new column is needed: an ignored/rejected offer must not be re-pestered for the cooldown window
    (plan §5.3). Tenant-predicated."""
    tid = str(tenant_id)
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    return PendingApprovalsWrapper().has_recent_of_type(
        tid, "autonomy_upgrade", within_days=int(cooldown_days), conn=conn
    )


def dispatch_autonomy_offer(
    tenant_id: UUID | str,
    agent: str,
    *,
    streak_count: int,
    send_fn: Any = None,
    dry_run: bool = False,
) -> str | None:
    """VT-384 §B2.1 — arm the L3 opt-in OFFER for an eligible (tenant, agent): send
    ``team_autonomy_offer`` to the owner AND open the ``autonomy_upgrade`` approval row (that row IS
    the C3 consent evidence; ``details.agent`` records which agent the grant will target). Goes
    through ``arm_pause_request`` so the per-tenant one-open-approval serialization (mig-128) holds:
    if ANOTHER approval is already open the arm is REFUSED and no offer is sent (returns None — the
    coordinator retries next sweep). Returns the approval_id on success, else None.

    Template params use the NAMED registry keys (owner_name {{1}}, streak_count {{2}}) — the
    same convention as ``arm_agent_send_approval``; the owner display name is an arm-time RLS read
    (reuses ``approval_glue._owner_display_name``, never logged). The offer is NOT a LangGraph
    interrupt — it is a fire-and-forget owner notice + a durable approval row the deterministic
    ENABLE handler later resolves (no run is paused on it). CL-390: IDs only in logs."""
    from uuid import uuid4 as _uuid4

    from orchestrator.agent.tools.request_owner_approval import (
        RequestOwnerApprovalInput,
        arm_pause_request,
    )
    from orchestrator.agents.approval_glue import _owner_display_name

    tid = str(tenant_id)
    # The offer has no agent run of its own, but pending_approvals.run_id FKs pipeline_runs — so
    # open a minimal provenance run row (run_type='autonomy_offer') the approval hangs off. Done in
    # the same arm-time RLS conn as the owner-name read.
    offer_run_id = _uuid4()
    with tenant_connection(tid) as c:
        owner_name = _owner_display_name(c, tid)
        c.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'autonomy_offer', 'running') ON CONFLICT (id) DO NOTHING",
            (str(offer_run_id), tid),
        )

    payload = RequestOwnerApprovalInput(
        tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tid)),
        run_id=offer_run_id,
        approval_type="autonomy_upgrade",
        summary=f"L3 autonomy offer for agent {agent} ({streak_count}-clean streak)",
        details={"agent": agent, "streak_count": int(streak_count)},
        template_name="team_autonomy_offer",
        template_params={"owner_name": owner_name, "streak_count": str(streak_count)},
    )
    result = arm_pause_request(payload, send_fn=send_fn, dry_run=dry_run)
    if result.status != "armed" or result.approval_id is None:
        logger.info(
            "dispatch_autonomy_offer: not armed tenant=%s agent=%s status=%s",
            tid, agent, result.status,
        )
        return None
    _emit(tid, "agent_autonomy_offer_sent",
          {"agent": agent, "approval_id": str(result.approval_id), "streak_count": int(streak_count)})
    return str(result.approval_id)


def resolve_and_grant_l3(
    tenant_id: UUID | str, approval_id: UUID | str, *, conn: Any
) -> tuple[str | None, AutonomyState | None]:
    """VT-384 ENABLE path — the owner's deterministic ENABLE reply to a ``team_autonomy_offer``.

    Resolve the open ``autonomy_upgrade`` approval (the C3 consent-evidence row) and grant L3 for
    the agent the offer was armed for, ATOMICALLY on the caller's ``conn``:
      1. read the approval row (must be OPEN + approval_type='autonomy_upgrade'); the offer stored
         the agent in ``details->>'agent'``;
      2. mark it resolved decision='approved' (tenant-predicated wrapper write);
      3. ``grant_l3(approval_id)`` — which RE-VALIDATES the streak/frozen/level IN-TXN (a stale
         grant no-ops; the row id is the durable consent evidence).

    Returns ``(agent, new_state)`` on success, ``(None, None)`` when there is no open
    autonomy_upgrade approval to act on (idempotent: a duplicate ENABLE after the grant finds
    nothing open → no-op). CL-390: IDs only — never the owner phone/body."""
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    tid = str(tenant_id)

    row = PendingApprovalsWrapper().get_open_by_id(tid, approval_id, conn=conn)
    if row is None:
        return None, None
    g = dict(row) if not isinstance(row, dict) else row
    if g.get("approval_type") != "autonomy_upgrade":
        return None, None
    details = g.get("details") or {}
    if not isinstance(details, dict):
        import json as _json

        try:
            details = _json.loads(details)
        except (TypeError, ValueError):
            details = {}
    agent = details.get("agent")
    if not agent:
        logger.warning(
            "resolve_and_grant_l3: autonomy_upgrade approval %s has no agent in details — no grant",
            approval_id,
        )
        return None, None
    PendingApprovalsWrapper().mark_resolved(
        tenant_id, approval_id, decision="approved", status="approved", conn=conn
    )
    state = grant_l3(tenant_id, agent, approval_id, conn=conn)
    return str(agent), state


def find_open_autonomy_upgrade(tenant_id: UUID | str, *, conn: Any) -> dict[str, Any] | None:
    """Return the most-recent OPEN ``autonomy_upgrade`` approval for the tenant (id + agent), else
    None — the ENABLE handler's lookup. Tenant-predicated (RLS via the caller's conn). The per-tenant
    one-open-approval index (mig-128) means at most one open approval exists at a time; this filters
    to the autonomy_upgrade type so a co-open agent_customer_send approval is not mistaken for it."""
    tid = str(tenant_id)
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    row = PendingApprovalsWrapper().latest_open_of_type(tid, "autonomy_upgrade", conn=conn)
    if row is None:
        return None
    g = dict(row) if not isinstance(row, dict) else row
    details = g.get("details") or {}
    if not isinstance(details, dict):
        import json as _json

        try:
            details = _json.loads(details)
        except (TypeError, ValueError):
            details = {}
    return {"id": g["id"], "agent": details.get("agent")}


def revoke_l3(tenant_id: UUID | str, agent: str, *, reason: str, conn: Any) -> AutonomyState:
    """Explicit revoke (owner cancel / VTR / Ops): L3 → L2, streak 0, cancels open batches
    atomically. Idempotent at L2 (still cancels batches — a revoke request means stop the work)."""
    from orchestrator.observability.tm_audit import emit_tm_audit
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    conn.execute(
        "UPDATE tenant_agent_autonomy SET level = 'L2', clean_approval_streak = 0, "
        "l3_revoked_at = now(), revoke_reason = %s, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s",
        (reason, tid, agent),
    )
    cancelled = cancel_open_batches(tenant_id, agent, reason=f"revoke_{reason}", conn=conn)
    emit_tm_audit(
        event_layer="does",
        event_kind="autonomy_change",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=None,
        action={"agent": agent, "reason": reason, "batches_cancelled": cancelled, "new_level": "L2"},
        summary=f"autonomy revoked L3→L2: agent={agent} reason={reason}",
        conn=conn,
    )
    _emit(tid, "agent_autonomy_revoked", {"agent": agent, "reason": reason,
                                          "batches_cancelled": cancelled})
    return get_autonomy(tenant_id, agent, conn=conn)


def set_frozen(tenant_id: UUID | str, agent: str, frozen: bool, *, reason: str, conn: Any) -> AutonomyState:
    """The kill switch (Ops/VTR). Freezing cancels open batches atomically (the binding rule);
    unfreezing cancels nothing (work re-enters via the next coordinator sweep)."""
    from orchestrator.observability.tm_audit import emit_tm_audit
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    conn.execute(
        "UPDATE tenant_agent_autonomy SET frozen = %s, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s",
        (frozen, tid, agent),
    )
    cancelled = cancel_open_batches(tenant_id, agent, reason=f"freeze_{reason}", conn=conn) if frozen else 0
    emit_tm_audit(
        event_layer="does",
        event_kind="autonomy_change",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=None,
        action={"agent": agent, "reason": reason, "frozen": frozen, "batches_cancelled": cancelled},
        summary=f"autonomy {'frozen' if frozen else 'unfrozen'}: agent={agent} reason={reason}",
        conn=conn,
    )
    _emit(tid, "agent_autonomy_frozen" if frozen else "agent_autonomy_unfrozen",
          {"agent": agent, "reason": reason, "batches_cancelled": cancelled})
    return get_autonomy(tenant_id, agent, conn=conn)


def vtr_autonomy_override(
    tenant_id: UUID | str, agent: str,
    action: Literal["freeze", "unfreeze", "demote", "revoke_l3", "force_l3"],
    *, reason: str, vtr_id: str, conn: Any,
) -> AutonomyState:
    """The Gap-6 seam: a VTR corrects/halts an agent. Thin provenance wrapper over the primitives
    (every action carries the vtr id in the reason; Gap-6 wires the VTR surface onto this).
    ``unfreeze`` (VT-370) dispatches to ``set_frozen(False)`` — without it the freeze button is
    one-way and recovery is psql; unfreezing cancels nothing (work re-enters via the next sweep).
    ``force_l3`` (VT-610) dispatches to ``force_l3`` — the ONLY action here that WIDENS trust
    rather than tightening/halting it; see that function's docstring for the bypass boundary."""
    tagged = f"vtr:{vtr_id}:{reason}"
    if action == "freeze":
        return set_frozen(tenant_id, agent, True, reason=tagged, conn=conn)
    if action == "unfreeze":
        return set_frozen(tenant_id, agent, False, reason=tagged, conn=conn)
    if action == "revoke_l3":
        return revoke_l3(tenant_id, agent, reason=tagged, conn=conn)
    if action == "force_l3":
        # force_l3 wants vtr_id and reason SEPARATELY (vtr_id is a durable column value, not a
        # tagged-into-reason string like the tighten/halt actions above use for revoke_reason).
        return force_l3(tenant_id, agent, vtr_id=vtr_id, reason=reason, conn=conn)
    # demote: revoke without freezing (back to L2, batches cancelled, owner can re-earn)
    return revoke_l3(tenant_id, agent, reason=f"demote:{tagged}", conn=conn)


def cancel_batch(
    tenant_id: UUID | str, batch_id: UUID | str, *, reason: str, vtr_id: str, conn: Any
) -> int:
    """VT-370: cancel ONE batch (the scalpel — correcting one bad batch must not nuke a healthy
    agent the way freeze does). The single-batch narrowing of cancel_open_batches: the batch →
    'cancelled' (only from a non-terminal state), its drafted rows → 'halted'. Returns the number
    of drafts halted; 0 also when the batch was already terminal (idempotent)."""
    tid, bid = str(tenant_id), str(batch_id)
    row = conn.execute(
        "UPDATE agent_draft_batches SET status = 'cancelled', updated_at = now() "
        "WHERE tenant_id = %s AND id = %s AND status = ANY(%s) RETURNING agent",
        (tid, bid, list(_OPEN_BATCH_STATUSES)),
    ).fetchone()
    if row is None:
        return 0
    agent = row["agent"] if isinstance(row, dict) else row[0]
    halted = conn.execute(
        "UPDATE agent_drafts SET status = 'halted', skip_reason = 'halted_vtr_cancel', "
        "updated_at = now() WHERE tenant_id = %s AND batch_id = %s AND status = 'drafted' "
        "RETURNING id",
        (tid, bid),
    ).fetchall()
    # VT-382 (CL-437.3): single-batch terminal cancel — redact owner_feedback + halted
    # draft params in the SAME txn.
    from orchestrator.agents.outbox_redaction import redact_batch_close

    redact_batch_close(conn, tid, [bid])
    _emit(tid, "agent_batch_cancelled", {"agent": str(agent), "batch_id": bid,
                                         "drafts_halted": len(halted),
                                         "by": f"vtr:{vtr_id}"})
    return len(halted)


def is_always_confirm(
    tenant_id: UUID | str, *, agent: str, batch_customer_ids: list[str],
    template_name: str, money_bearing: bool, conn: Any,
) -> tuple[bool, str]:
    """The §5.5 always-confirm floor — re-derived PER BATCH at the send choke point (PR-3 gate 1c;
    the stored level column is routing metadata, never trusted at send). True for ANY of:
    first-contact (a customer with NO prior agent_customer_contacts row), bulk (> L3_AUTO_MAX_BATCH),
    money (a money_bearing template), novel template (never sent by this tenant before)."""
    tid = str(tenant_id)
    if money_bearing:
        return True, "money_template"
    if len(batch_customer_ids) > L3_AUTO_MAX_BATCH:
        return True, "bulk"
    if batch_customer_ids:
        row = conn.execute(
            "SELECT count(*) FROM (SELECT unnest(%s::uuid[]) AS cid) want "
            "WHERE NOT EXISTS (SELECT 1 FROM agent_customer_contacts acc "
            "WHERE acc.tenant_id = %s AND acc.customer_id = want.cid)",
            (batch_customer_ids, tid),
        ).fetchone()
        first_contacts = int(row[0] if not isinstance(row, dict) else row["count"])
        if first_contacts > 0:
            return True, "first_contact"
    row = conn.execute(
        "SELECT 1 FROM agent_customer_contacts WHERE tenant_id = %s AND template_name = %s LIMIT 1",
        (tid, template_name),
    ).fetchone()
    if row is None:
        return True, "novel_template"
    return False, ""


def _emit(tenant_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort observability — never fails the caller's transaction."""
    try:
        from orchestrator.observability.log import log_event

        log_event(event_type=event_type, run_id=uuid4(), tenant_id=UUID(tenant_id),
                  severity="info", component="agents",
                  payload={"tenant_id": tenant_id, **payload})
    except Exception:  # noqa: BLE001
        logger.exception("autonomy: %s emit failed tenant=%s", event_type, tenant_id)
