"""VT-467 — the business-impact rails framework (extends the VT-460 rail harness).

VT-460 gated the CUSTOMER-SEND side effect: the brain holds no send tool (VT-268 capability guard),
every customer send routes a deterministic gate stack, and the transport fails closed for an un-gated
customer send (``customer_send_context`` + ``UngatedCustomerSendError``). VT-467 GENERALIZES that
exact structure to the other CONSEQUENTIAL business-impact actions a full-six manager can take:

  * SPEND        — commit the owner's money (a paid campaign, a vendor purchase, a subscription).
  * COMMITMENT   — make an external commitment on the owner's behalf (a quote, a booking, a contract).
  * CONFIG       — change an integration/config the owner depends on (a listing edit, a connector
                   re-wire, a store/website setting).

(customer SEND is DELIBERATELY NOT a class here — VT-460 owns it. The two harnesses compose; they do
not overlap.)

THE CONTRACT (design §4 + §7, non-negotiable)
---------------------------------------------------------------------------
"Nothing hardcoded" = dynamic BEHAVIOUR; the safety/correctness RAILS stay DETERMINISTIC and
non-bypassable. The manager/specialist emits an INTENT ("spend ₹500 on this boost"); the EFFECT runs
through a guarded tool that deterministically decides AUTONOMOUS vs REQUIRES_OWNER_APPROVAL from
{action class, magnitude, tenant autonomy tier}. The manager has NO code path to a consequential side
effect except via this gate (mirror of VT-460's structural choke + VT-268's capability guard).

THREE structural boundaries (mirroring VT-460's two, plus the gate):
  1. THE GATE — ``assert_or_gate_business_action`` decides autonomous-vs-approval DETERMINISTICALLY
     (never the brain's vibe). Below threshold + a permitting tier → autonomous; at/above, or a
     low-autonomy/frozen tenant → REQUIRES_OWNER_APPROVAL, routed through the EXISTING owner-approval
     machinery (``arm_pause_request`` → ``request_owner_approval_node`` → ``route_after_approval`` —
     the SAME Pillar-7 interrupt/resume path ``agent_customer_send`` uses; NOT a parallel flow).
  2. THE TRANSPORT CHOKE — ``business_action_context`` (a contextvar) + ``UngatedBusinessActionError``.
     A side-effecting business action FLAGGED consequential, attempted OUTSIDE the gated context,
     fails CLOSED before the effect. This is the structural backstop: a future direct caller that
     forgets the gate raises rather than silently spending/committing/reconfiguring. Generalizes
     ``twilio_send.customer_send_context`` / ``UngatedCustomerSendError`` to non-send effects.
  3. THE CAPABILITY GUARD — the brain must not HOLD a tool that performs SPEND/COMMIT/CONFIG directly;
     the VT-268 ``assert_agent_tools_safe`` forbidden-substring set is EXTENDED (not reinvented) so a
     spend/commit/config-write tool on the agent surface raises at graph build.

DECAYING-HITL (design §7 — REUSES the VTR/L2-L3 model's SHAPE)
---------------------------------------------------------------------------
The approval requirement LOOSENS as the owner grants the manager more autonomy + it earns trust.
Per-tenant, per-action-class, DETERMINISTIC — stored in ``tenant_business_autonomy`` (migration 143),
the business-impact analogue of ``tenant_agent_autonomy`` (the customer-send L2/L3 model). A MISSING
row IS the fail-closed floor (``always_approve``): a tenant with no explicit grant gets owner approval
on EVERY business-impact action, exactly like a missing tenant_agent_autonomy row is L2. The owner
LOOSENS by raising the tier / the threshold (``grant_business_autonomy``); a regression/kill TIGHTENS
(``freeze_business_class`` / a 'frozen' row → back to always-approve). Same monotonic-trust intuition
as L2→L3, on a magnitude axis instead of a send-count streak.

CL-390: IDs + action class + boolean/magnitude codes only in logs — never an owner secret, never a
free-form owner instruction body.
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator
from uuid import UUID

logger = logging.getLogger(__name__)


# ===========================================================================
# The action taxonomy
# ===========================================================================


class BusinessImpactClass(str, Enum):
    """The consequential business-impact action classes VT-467 gates.

    customer SEND is NOT here — VT-460 owns it. These three are the new surface (design §7):
      * ``SPEND``       — commit the owner's money.
      * ``COMMITMENT``  — make an external commitment on the owner's behalf.
      * ``CONFIG``      — change an integration/config the owner depends on.

    The str values match the ``action_class`` CHECK in migration 143 (the DB enum is the source of
    truth in sync with this — same PR).
    """

    SPEND = "spend"
    COMMITMENT = "commitment"
    CONFIG = "config"


class BusinessActionDecision(str, Enum):
    """The deterministic gate outcome. Exactly two terminal decisions — no third 'maybe'."""

    AUTONOMOUS = "autonomous"                      # within policy + below threshold + tier permits
    REQUIRES_OWNER_APPROVAL = "requires_owner_approval"  # everything else (fail-closed default)


# Deterministic decision REASON markers (CL-390: a code, never an instruction body).
REASON_NO_AUTONOMY_SETTING = "no_autonomy_setting"   # fail-closed default — no grant exists
REASON_FROZEN = "frozen"                             # the class is frozen (kill switch)
REASON_ALWAYS_APPROVE_TIER = "always_approve_tier"   # tier explicitly requires approval for the class
REASON_AT_OR_ABOVE_THRESHOLD = "at_or_above_threshold"  # magnitude >= the autonomous threshold
REASON_ABOVE_CEILING = "above_ceiling"               # magnitude > the autonomous tier's hard ceiling
REASON_BELOW_THRESHOLD = "below_threshold"           # autonomous: magnitude < threshold, tier permits
REASON_WITHIN_CEILING = "within_ceiling"             # autonomous: magnitude <= ceiling, tier autonomous
REASON_NEGATIVE_MAGNITUDE = "negative_magnitude"     # guard: a negative magnitude is never autonomous


# The autonomy tiers (mirror the migration CHECK). A MISSING row reads as ALWAYS_APPROVE (fail-closed).
TIER_ALWAYS_APPROVE = "always_approve"
TIER_THRESHOLD = "threshold"
TIER_AUTONOMOUS = "autonomous"


# ===========================================================================
# The decaying-HITL state — per-(tenant, action_class), fail-closed default
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BusinessAutonomyState:
    """The deterministic autonomy state the gate reads. A MISSING row is constructed as the
    fail-closed floor: tier=always_approve, no threshold, not frozen — owner approval on everything."""

    tenant_id: UUID
    action_class: str
    tier: str = TIER_ALWAYS_APPROVE
    auto_approve_below_minor: int | None = None
    autonomous_ceiling_minor: int | None = None
    frozen: bool = False


_SELECT_AUTONOMY = (
    "SELECT tier, auto_approve_below_minor, autonomous_ceiling_minor, frozen "
    "FROM tenant_business_autonomy WHERE tenant_id = %s AND action_class = %s"
)


def _row_to_state(tenant_id: UUID, action_class: str, row: Any) -> BusinessAutonomyState:
    """Build the state from a psycopg row (dict or tuple). ``None`` row → the fail-closed default."""
    if row is None:
        return BusinessAutonomyState(tenant_id=tenant_id, action_class=action_class)
    g = dict(row) if isinstance(row, dict) else dict(
        zip(("tier", "auto_approve_below_minor", "autonomous_ceiling_minor", "frozen"), row,
            strict=False)
    )
    below = g.get("auto_approve_below_minor")
    ceil = g.get("autonomous_ceiling_minor")
    return BusinessAutonomyState(
        tenant_id=tenant_id,
        action_class=action_class,
        tier=str(g["tier"]),
        auto_approve_below_minor=int(below) if below is not None else None,
        autonomous_ceiling_minor=int(ceil) if ceil is not None else None,
        frozen=bool(g["frozen"]),
    )


def get_business_autonomy(
    tenant_id: UUID | str,
    action_class: BusinessImpactClass | str,
    *,
    conn: Any = None,
) -> BusinessAutonomyState:
    """Read the per-(tenant, action_class) business-autonomy state.

    FAIL-CLOSED: a MISSING row → ``always_approve`` (owner approval on everything). This is the
    structural default — a tenant with no explicit owner grant is the most-restrictive tier, exactly
    like a missing ``tenant_agent_autonomy`` row is L2. ``conn`` is the caller's RLS-scoped
    ``tenant_connection``; RLS independently confirms the tenant.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    ac = action_class.value if isinstance(action_class, BusinessImpactClass) else str(action_class)
    if conn is not None:
        row = conn.execute(_SELECT_AUTONOMY, (str(tid), ac)).fetchone()
    else:
        from orchestrator.db import tenant_connection

        with tenant_connection(tid) as c:
            row = c.execute(_SELECT_AUTONOMY, (str(tid), ac)).fetchone()
    return _row_to_state(tid, ac, row)


# ===========================================================================
# THE GATE — deterministic autonomous-vs-approval
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BusinessActionGate:
    """The deterministic gate outcome. PII-safe (ids + class + magnitude + a reason CODE only)."""

    decision: BusinessActionDecision
    reason: str
    action_class: str
    magnitude_minor: int
    tier: str

    @property
    def autonomous(self) -> bool:
        return self.decision is BusinessActionDecision.AUTONOMOUS

    @property
    def requires_owner_approval(self) -> bool:
        return self.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL


def decide_business_action(
    state: BusinessAutonomyState, magnitude_minor: int
) -> BusinessActionGate:
    """The PURE deterministic decision function (no I/O — testable in isolation, the rail's core).

    The decision ladder (fail-closed at every branch):
      0. magnitude < 0          → REQUIRES_OWNER_APPROVAL (a negative magnitude is never autonomous —
                                  a refund/credit is still a consequential action; never auto-pass it).
      1. frozen                 → REQUIRES_OWNER_APPROVAL (the kill switch overrides the tier).
      2. tier always_approve    → REQUIRES_OWNER_APPROVAL (the fail-closed default + an explicit floor).
      3. tier threshold         → autonomous iff magnitude < auto_approve_below_minor (a NULL/0
                                  threshold means autonomous nowhere — fail-closed); at/above → approve.
      4. tier autonomous        → autonomous iff magnitude <= autonomous_ceiling_minor (a NULL ceiling
                                  = no ceiling = autonomous for any magnitude); above → approve (the
                                  "extreme scenario" escalation line, §6).
    Any unknown tier (a future DB value this code predates) falls through to REQUIRES_OWNER_APPROVAL.
    """
    ac, tier = state.action_class, state.tier

    def _gate(decision: BusinessActionDecision, reason: str) -> BusinessActionGate:
        return BusinessActionGate(
            decision=decision, reason=reason, action_class=ac,
            magnitude_minor=magnitude_minor, tier=tier,
        )

    # 0. A negative magnitude is never autonomous (fail-closed — refunds/credits are consequential).
    if magnitude_minor < 0:
        return _gate(BusinessActionDecision.REQUIRES_OWNER_APPROVAL, REASON_NEGATIVE_MAGNITUDE)

    # 1. The kill switch wins over the tier.
    if state.frozen:
        return _gate(BusinessActionDecision.REQUIRES_OWNER_APPROVAL, REASON_FROZEN)

    # 2. The fail-closed default / explicit always-approve floor.
    if tier == TIER_ALWAYS_APPROVE:
        return _gate(BusinessActionDecision.REQUIRES_OWNER_APPROVAL, REASON_ALWAYS_APPROVE_TIER)

    # 3. Threshold tier: autonomous strictly below the threshold; NULL threshold = autonomous nowhere.
    if tier == TIER_THRESHOLD:
        below = state.auto_approve_below_minor
        if below is not None and magnitude_minor < below:
            return _gate(BusinessActionDecision.AUTONOMOUS, REASON_BELOW_THRESHOLD)
        return _gate(BusinessActionDecision.REQUIRES_OWNER_APPROVAL, REASON_AT_OR_ABOVE_THRESHOLD)

    # 4. Autonomous tier: autonomous up to (and including) the ceiling; NULL ceiling = no ceiling.
    if tier == TIER_AUTONOMOUS:
        ceil = state.autonomous_ceiling_minor
        if ceil is None or magnitude_minor <= ceil:
            return _gate(BusinessActionDecision.AUTONOMOUS, REASON_WITHIN_CEILING)
        return _gate(BusinessActionDecision.REQUIRES_OWNER_APPROVAL, REASON_ABOVE_CEILING)

    # Unknown tier (forward-compat): fail closed.
    return _gate(BusinessActionDecision.REQUIRES_OWNER_APPROVAL, REASON_NO_AUTONOMY_SETTING)


def assert_or_gate_business_action(
    tenant_id: UUID | str,
    action_class: BusinessImpactClass | str,
    magnitude_minor: int,
    *,
    action_attrs: dict[str, Any] | None = None,
    conn: Any = None,
) -> BusinessActionGate:
    """THE entry gate every consequential business-impact action MUST pass before its effect.

    DETERMINISTICALLY decides AUTONOMOUS vs REQUIRES_OWNER_APPROVAL from {action class, magnitude,
    the tenant's per-class autonomy tier}. Reads ``tenant_business_autonomy`` (fail-closed: no row →
    REQUIRES_OWNER_APPROVAL on everything). NEVER raises for a gate decision — it RETURNS the gate;
    the caller acts on ``gate.autonomous`` (proceed inside ``business_action_context``) vs
    ``gate.requires_owner_approval`` (route through ``arm_business_action_approval``).

    The brain/specialist has NO code path to execute a consequential action except via this gate +
    the ``business_action_context`` choke — the structural mirror of VT-460.

    VT-474 A2 — the OUTER policy bound. When the caller supplies ``action_attrs`` (the intent's
    machine-checkable fields — segment / magnitude / freq-cap-key+count), this gate FIRST runs the
    deterministic ``assert_within_policy`` bound-check. An OUT_OF_POLICY action is forced to
    REQUIRES_OWNER_APPROVAL **regardless of the magnitude tier** — the brain cannot tier/threshold its
    way past the owner's policy (allowed action-types / segments / spend ceiling / freq caps). The
    policy check is the OUTER bound; the per-class autonomy tier is the INNER autonomous-vs-approval
    decay BENEATH it. ``action_attrs`` omitted ⇒ tier-only (unchanged) — the policy is enforced where
    the action path provides the intent attrs (the real consequential callers do; a tier-only
    diagnostic call is unaffected). The policy short-circuit is fail-CLOSED: no policy row → every
    attrs-bearing action is OUT_OF_POLICY → owner approval.
    """
    from orchestrator.observability.tm_audit import emit_tm_audit
    if action_attrs is not None:
        from orchestrator.agents.business_policy import assert_within_policy

        check = assert_within_policy(tenant_id, action_class, action_attrs, conn=conn)
        if check.out_of_policy:
            # Out-of-policy is forced to owner approval irrespective of tier/magnitude (A2: the brain
            # cannot reason itself out of policy). Carry the policy reason so the owner-approval ask +
            # the audit log record WHICH bound was breached.
            ac = action_class.value if isinstance(action_class, BusinessImpactClass) else str(action_class)
            state = get_business_autonomy(tenant_id, action_class, conn=conn)
            gate = BusinessActionGate(
                decision=BusinessActionDecision.REQUIRES_OWNER_APPROVAL,
                reason=f"out_of_policy:{check.reason}",
                action_class=ac,
                magnitude_minor=magnitude_minor,
                tier=state.tier,
            )
            logger.info(
                "business_impact_choke: gate tenant=%s class=%s magnitude_minor=%d tier=%s "
                "decision=%s reason=%s (policy-bound)",
                str(tenant_id), gate.action_class, gate.magnitude_minor, gate.tier,
                gate.decision.value, gate.reason,
            )
            emit_tm_audit(
                event_layer="decides",
                event_kind="business_action",
                actor="team_manager",
                tenant_id=tenant_id,
                run_id=None,
                decision={
                    "action_class": gate.action_class,
                    "magnitude_minor": gate.magnitude_minor,
                    "tier": gate.tier,
                    "gate_decision": gate.decision.value,
                    "reason": gate.reason,
                },
                summary=f"business action gated: {gate.action_class} → {gate.decision.value} ({gate.reason})",
                conn=None,
            )
            return gate

    state = get_business_autonomy(tenant_id, action_class, conn=conn)
    gate = decide_business_action(state, magnitude_minor)
    logger.info(
        "business_impact_choke: gate tenant=%s class=%s magnitude_minor=%d tier=%s decision=%s reason=%s",
        str(tenant_id), gate.action_class, gate.magnitude_minor, gate.tier,
        gate.decision.value, gate.reason,
    )
    emit_tm_audit(
        event_layer="decides",
        event_kind="business_action",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=None,
        decision={
            "action_class": gate.action_class,
            "magnitude_minor": gate.magnitude_minor,
            "tier": gate.tier,
            "gate_decision": gate.decision.value,
            "reason": gate.reason,
        },
        summary=f"business action gated: {gate.action_class} → {gate.decision.value} ({gate.reason})",
        conn=None,
    )
    return gate


# ===========================================================================
# THE TRANSPORT CHOKE — fail-closed structural boundary at the effect
# ===========================================================================
#
# Generalizes twilio_send.customer_send_context / UngatedCustomerSendError to NON-send business
# effects. A consequential action's effect (a payment call, a commitment write, a config push) MUST
# be issued from INSIDE business_action_context() — which the gate-approved path enters AFTER the
# deterministic decision (autonomous) OR after the owner approves (approval path). A direct caller
# that performs a flagged business effect but forgot the gate raises UngatedBusinessActionError
# rather than silently committing the effect: the boundary is STRUCTURAL, not convention + review.

_GATED_BUSINESS_ACTION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "viabe_gated_business_action", default=None
)


class UngatedBusinessActionError(RuntimeError):
    """Raised when a consequential business-impact effect is attempted OUTSIDE
    ``business_action_context()``.

    The structural backstop (the VT-467 analogue of VT-460 gap c): a SPEND/COMMITMENT/CONFIG effect
    that did not route through the deterministic gate fails CLOSED before the effect, rather than
    silently spending/committing/reconfiguring. A gate-approved or owner-approved path enters the
    context first; an un-gated direct caller trips this.
    """


@contextmanager
def business_action_context(action_class: BusinessImpactClass | str) -> Iterator[None]:
    """Mark the dynamic extent of a GATED business-impact effect.

    Entered ONLY by a caller that has already run ``assert_or_gate_business_action`` and either got
    AUTONOMOUS, or got the owner's approval. The transport-level guard
    (``assert_in_business_action_context``) permits a flagged effect only while this is active.
    Re-entrant; the token restores the prior value on exit (a nested config-during-spend is fine).
    """
    ac = action_class.value if isinstance(action_class, BusinessImpactClass) else str(action_class)
    token = _GATED_BUSINESS_ACTION.set(ac)
    try:
        yield
    finally:
        _GATED_BUSINESS_ACTION.reset(token)


def assert_in_business_action_context(action_class: BusinessImpactClass | str) -> None:
    """Fail-CLOSED guard a side-effecting business action MUST call IMMEDIATELY before its effect.

    Raises ``UngatedBusinessActionError`` if called outside ``business_action_context`` (i.e. the
    effect was reached without passing the gate). The structural choke: a tool that performs
    SPEND/COMMIT/CONFIG must route through the gate or this raises — it cannot silently take effect.
    """
    ac = action_class.value if isinstance(action_class, BusinessImpactClass) else str(action_class)
    active = _GATED_BUSINESS_ACTION.get()
    if active is None:
        raise UngatedBusinessActionError(
            f"un-gated business-impact action refused at the effect boundary: class={ac!r}. "
            "Consequential actions (SPEND / COMMITMENT / CONFIG) MUST route through "
            "assert_or_gate_business_action() then business_action_context() (VT-467); a direct "
            "effect that skips the gate is a structural boundary breach."
        )


# ===========================================================================
# THE APPROVAL ROUTE — REUSE the existing owner-approval machinery (no fork)
# ===========================================================================


def arm_business_action_approval(
    tenant_id: UUID | str,
    run_id: UUID | str,
    gate: BusinessActionGate,
    *,
    summary: str,
    details: dict[str, Any] | None = None,
    conn: Any = None,
    send_fn: Any | None = None,
    dry_run: bool = False,
) -> Any:
    """Route a REQUIRES_OWNER_APPROVAL business action through the EXISTING owner-approval flow.

    Arms a ``pending_approvals`` row of type ``business_impact_action`` via ``arm_pause_request`` —
    the SINGLE arming path (it owns the per-tenant one-open-approval serialization + the migration-128
    backstop) that ``agent_customer_send`` also uses. This is NOT a parallel approval flow: the same
    interrupt/resume Pillar-7 machinery (``request_owner_approval_node`` → owner reply →
    ``route_after_approval``) carries it. Returns the ``PauseRequestResult``; the caller checks
    ``status == 'armed'`` and only proceeds to the effect after the owner's 'approved' decision.

    ``details`` carries the action class + magnitude (the gate's PII-safe fields) so the resume path
    can re-derive the decision; it MUST NOT carry an owner secret or a free-form instruction body
    (CL-390). The ``summary`` is owner-facing (≤500 chars) and likewise PII-light.
    """
    from orchestrator.agent.tools.request_owner_approval import (
        RequestOwnerApprovalInput,
        arm_pause_request,
    )

    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    rid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))

    safe_details: dict[str, Any] = {
        "action_class": gate.action_class,
        "magnitude_minor": int(gate.magnitude_minor),
        "tier": gate.tier,
        "gate_reason": gate.reason,
    }
    if details:
        safe_details.update(details)

    payload = RequestOwnerApprovalInput(
        tenant_id=tid,
        run_id=rid,
        approval_type="business_impact_action",
        summary=summary,
        details=safe_details,
    )
    conn_factory = None
    if conn is not None:
        from contextlib import contextmanager as _cm

        @_cm
        def _reuse(_t: UUID | str):  # caller-owned conn: do not close it
            yield conn

        conn_factory = _reuse

    result = arm_pause_request(
        payload, conn_factory=conn_factory, send_fn=send_fn, dry_run=dry_run
    )
    logger.info(
        "business_impact_choke: armed business_impact_action tenant=%s class=%s status=%s",
        str(tid), gate.action_class, result.status,
    )
    return result


# ===========================================================================
# THE DECAYING-HITL primitives — deterministic loosen (grant) / tighten (freeze)
# ===========================================================================


def grant_business_autonomy(
    tenant_id: UUID | str,
    action_class: BusinessImpactClass | str,
    *,
    tier: str,
    auto_approve_below_minor: int | None = None,
    autonomous_ceiling_minor: int | None = None,
    granted_by_approval_id: UUID | str | None = None,
    conn: Any,
) -> BusinessAutonomyState:
    """The owner LOOSENS autonomy for a class (the decay — the approval requirement relaxes).

    DETERMINISTIC: sets the tier + threshold/ceiling the gate reads. This is an explicit OWNER act
    (it carries the owner's approval-row provenance), never the brain's choice — a specialist can
    PROPOSE a grant (an owner approval) but only the owner's 'approved' resolution calls this. Upsert
    on (tenant_id, action_class); clears ``frozen`` (a grant un-freezes the class). MUST run on the
    caller's RLS-scoped ``conn``.
    """
    tid = str(tenant_id)
    ac = action_class.value if isinstance(action_class, BusinessImpactClass) else str(action_class)
    if tier not in (TIER_ALWAYS_APPROVE, TIER_THRESHOLD, TIER_AUTONOMOUS):
        raise ValueError(f"unknown business-autonomy tier {tier!r}")
    grant_id = str(granted_by_approval_id) if granted_by_approval_id is not None else None
    conn.execute(
        "INSERT INTO tenant_business_autonomy "
        "(tenant_id, action_class, tier, auto_approve_below_minor, autonomous_ceiling_minor, "
        " frozen, granted_by_approval_id, granted_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, false, %s, now(), now()) "
        "ON CONFLICT (tenant_id, action_class) DO UPDATE SET "
        "tier = EXCLUDED.tier, "
        "auto_approve_below_minor = EXCLUDED.auto_approve_below_minor, "
        "autonomous_ceiling_minor = EXCLUDED.autonomous_ceiling_minor, "
        "frozen = false, "
        "granted_by_approval_id = EXCLUDED.granted_by_approval_id, "
        "granted_at = now(), updated_at = now()",
        (tid, ac, tier, auto_approve_below_minor, autonomous_ceiling_minor, grant_id),
    )
    logger.info(
        "business_impact_choke: granted tenant=%s class=%s tier=%s below=%s ceiling=%s",
        tid, ac, tier, auto_approve_below_minor, autonomous_ceiling_minor,
    )
    return get_business_autonomy(tenant_id, action_class, conn=conn)


def freeze_business_class(
    tenant_id: UUID | str,
    action_class: BusinessImpactClass | str,
    frozen: bool,
    *,
    reason: str,
    conn: Any,
) -> BusinessAutonomyState:
    """The kill switch (owner keyword / VTR override / Ops): TIGHTEN a class back to always-approve.

    ``frozen=True`` makes the gate return REQUIRES_OWNER_APPROVAL for the class regardless of tier
    (the regression/decay-tighten path — mirrors ``tenant_agent_autonomy.frozen``). Upserts a row if
    none exists (a freeze on a never-granted class is the always-approve floor anyway, but we record
    the kill explicitly). MUST run on the caller's RLS-scoped ``conn``.
    """
    tid = str(tenant_id)
    ac = action_class.value if isinstance(action_class, BusinessImpactClass) else str(action_class)
    conn.execute(
        "INSERT INTO tenant_business_autonomy "
        "(tenant_id, action_class, tier, frozen, last_regression_at, last_regression_reason, updated_at) "
        "VALUES (%s, %s, %s, %s, now(), %s, now()) "
        "ON CONFLICT (tenant_id, action_class) DO UPDATE SET "
        "frozen = EXCLUDED.frozen, "
        "last_regression_at = now(), last_regression_reason = EXCLUDED.last_regression_reason, "
        "updated_at = now()",
        (tid, ac, TIER_ALWAYS_APPROVE, frozen, reason),
    )
    logger.info(
        "business_impact_choke: %s tenant=%s class=%s reason=%s",
        "froze" if frozen else "unfroze", tid, ac, reason,
    )
    return get_business_autonomy(tenant_id, action_class, conn=conn)


__all__ = [
    "BusinessImpactClass",
    "BusinessActionDecision",
    "BusinessAutonomyState",
    "BusinessActionGate",
    "UngatedBusinessActionError",
    "TIER_ALWAYS_APPROVE",
    "TIER_THRESHOLD",
    "TIER_AUTONOMOUS",
    "REASON_NO_AUTONOMY_SETTING",
    "REASON_FROZEN",
    "REASON_ALWAYS_APPROVE_TIER",
    "REASON_AT_OR_ABOVE_THRESHOLD",
    "REASON_ABOVE_CEILING",
    "REASON_BELOW_THRESHOLD",
    "REASON_WITHIN_CEILING",
    "REASON_NEGATIVE_MAGNITUDE",
    "get_business_autonomy",
    "decide_business_action",
    "assert_or_gate_business_action",
    "business_action_context",
    "assert_in_business_action_context",
    "arm_business_action_approval",
    "grant_business_autonomy",
    "freeze_business_class",
]
