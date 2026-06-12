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
    last_regression_kind: str | None = None


def _row_to_state(tenant_id: UUID, agent: str, row: Any) -> AutonomyState:
    if row is None:
        return AutonomyState(tenant_id, agent, "L2", 0, 0, 0, False)
    g = dict(row) if isinstance(row, dict) else None
    if g is None:
        cols = ("level", "clean_approval_streak", "lifetime_approvals", "lifetime_rejections",
                "frozen", "l3_granted_at", "l3_grant_approval_id", "last_regression_kind")
        g = dict(zip(cols, row, strict=False))
    return AutonomyState(
        tenant_id=tenant_id, agent=agent, level=g["level"],
        clean_approval_streak=int(g["clean_approval_streak"]),
        lifetime_approvals=int(g["lifetime_approvals"]),
        lifetime_rejections=int(g["lifetime_rejections"]),
        frozen=bool(g["frozen"]),
        l3_granted_at=g.get("l3_granted_at"),
        l3_grant_approval_id=str(g["l3_grant_approval_id"]) if g.get("l3_grant_approval_id") else None,
        last_regression_kind=g.get("last_regression_kind"),
    )


_SELECT = (
    "SELECT level, clean_approval_streak, lifetime_approvals, lifetime_rejections, frozen, "
    "l3_granted_at, l3_grant_approval_id, last_regression_kind "
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
    tenant_id: UUID | str, agent: str, kind: RegressionKind, *, conn: Any
) -> AutonomyState:
    """Apply the §5.4 regression table on the caller's conn (same-txn discipline):
    streak → 0 always; lifetime_rejections +1 on 'reject'; FREEZE on the kill-switch kinds;
    at L3 → REVOKE to L2 (one-way per incident). Every revoke/freeze cancels open batches
    atomically. Emits an observability event (best-effort)."""
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
    _emit(tid, "agent_autonomy_regressed", {"agent": agent, "kind": kind,
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
        _emit(tid, "agent_autonomy_granted", {"agent": agent, "approval_id": str(approval_id)})
    return get_autonomy(tenant_id, agent, conn=conn)


def revoke_l3(tenant_id: UUID | str, agent: str, *, reason: str, conn: Any) -> AutonomyState:
    """Explicit revoke (owner cancel / VTR / Ops): L3 → L2, streak 0, cancels open batches
    atomically. Idempotent at L2 (still cancels batches — a revoke request means stop the work)."""
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    conn.execute(
        "UPDATE tenant_agent_autonomy SET level = 'L2', clean_approval_streak = 0, "
        "l3_revoked_at = now(), revoke_reason = %s, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s",
        (reason, tid, agent),
    )
    cancelled = cancel_open_batches(tenant_id, agent, reason=f"revoke_{reason}", conn=conn)
    _emit(tid, "agent_autonomy_revoked", {"agent": agent, "reason": reason,
                                          "batches_cancelled": cancelled})
    return get_autonomy(tenant_id, agent, conn=conn)


def set_frozen(tenant_id: UUID | str, agent: str, frozen: bool, *, reason: str, conn: Any) -> AutonomyState:
    """The kill switch (Ops/VTR). Freezing cancels open batches atomically (the binding rule);
    unfreezing cancels nothing (work re-enters via the next coordinator sweep)."""
    tid = str(tenant_id)
    _ensure_row(conn, tid, agent)
    conn.execute(
        "UPDATE tenant_agent_autonomy SET frozen = %s, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s",
        (frozen, tid, agent),
    )
    cancelled = cancel_open_batches(tenant_id, agent, reason=f"freeze_{reason}", conn=conn) if frozen else 0
    _emit(tid, "agent_autonomy_frozen" if frozen else "agent_autonomy_unfrozen",
          {"agent": agent, "reason": reason, "batches_cancelled": cancelled})
    return get_autonomy(tenant_id, agent, conn=conn)


def vtr_autonomy_override(
    tenant_id: UUID | str, agent: str,
    action: Literal["freeze", "unfreeze", "demote", "revoke_l3"],
    *, reason: str, vtr_id: str, conn: Any,
) -> AutonomyState:
    """The Gap-6 seam: a VTR corrects/halts an agent. Thin provenance wrapper over the primitives
    (every action carries the vtr id in the reason; Gap-6 wires the VTR surface onto this).
    ``unfreeze`` (VT-370) dispatches to ``set_frozen(False)`` — without it the freeze button is
    one-way and recovery is psql; unfreezing cancels nothing (work re-enters via the next sweep)."""
    tagged = f"vtr:{vtr_id}:{reason}"
    if action == "freeze":
        return set_frozen(tenant_id, agent, True, reason=tagged, conn=conn)
    if action == "unfreeze":
        return set_frozen(tenant_id, agent, False, reason=tagged, conn=conn)
    if action == "revoke_l3":
        return revoke_l3(tenant_id, agent, reason=tagged, conn=conn)
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
