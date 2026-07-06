"""VT-474 A2 — the deterministic, machine-enforceable BUSINESS POLICY bound-check.

The A2 ruling (design §8, Cowork hardening):

    "within policy" = a DETERMINISTIC bound-check (a rail/guard), NOT the brain's self-judgment.
    The onboarding-granted policy = machine-enforceable BOUNDS (segments, frequency caps, spend
    ceiling, allowed action-types). The brain cannot reason itself out of policy.

So this module is a PURE deterministic guard — zero LLM (Pillar 1). The manager/specialist emits an
action INTENT; ``assert_within_policy(tenant_id, action_class, action_attrs)`` returns IN_POLICY or
OUT_OF_POLICY(reason). The action paths (the VT-467 business-impact gate + the VT-460 customer-send
pre-gate) route through it: an out-of-policy action is GATED/ESCALATED, never executed. The brain has
NO code path to make the policy decision itself — it is read off the tenant's stored policy and bound-
checked here, deterministically.

COMPOSITION with the EXISTING rails (no parallel system — VT-474 is the OUTER bound):
  - VT-460 customer-send choke (onboarded + WABA + per-recipient consent/opt-out/caps) — UNCHANGED,
    still binds. Policy is an ADDITIONAL outer bound (is this segment/action even allowed + within the
    owner's freq cap), checked BEFORE the existing per-recipient gates.
  - VT-467 business-impact gate (per-(tenant, class) magnitude tier → autonomous vs approval) —
    UNCHANGED. Policy is the OUTER bound: is the action TYPE allowed at all + is its magnitude within
    the owner's spend ceiling. An out-of-policy business action is forced to REQUIRES_OWNER_APPROVAL
    REGARDLESS of the magnitude tier — the brain cannot tier its way past the policy.

FAIL-CLOSED (the A2 hardening, structural):
  - No policy row → the MOST-RESTRICTIVE policy (``_DENY_ALL``): no action type allowed, no segment
    allowed, every cap 0, spend ceiling 0. A tenant with no explicit owner grant can take NO
    autonomous business action — every intent is OUT_OF_POLICY.
  - Every ABSENT/empty/malformed field in a stored policy is its DENY value (an unknown action type
    is not in ``allowed_action_types`` ⇒ denied; a missing freq-cap key ⇒ 0 ⇒ denied; a non-numeric
    spend_ceiling ⇒ 0 ⇒ denied). A partial or corrupt policy can never silently WIDEN authority.

CL-390: IDs + action class + reason CODE + numeric bounds only in logs/results — never an owner
secret, never a free-form owner instruction body, never a customer phone/name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


def _action_class_value(action_class: PolicyActionClass | Enum | str) -> str:
    """Coerce any action-class spelling to its canonical string VALUE.

    Robust to a FOREIGN str-Enum (e.g. ``BusinessImpactClass.SPEND`` passed by the VT-467 gate):
    ``str()`` on a str-Enum yields the qualified NAME ('BusinessImpactClass.SPEND'), NOT the value
    ('spend'), in Python 3.11+. Reading ``.value`` for any Enum avoids that trap; a bare str passes
    through. Without this the gate's policy check would compare 'BusinessImpactClass.SPEND' against
    the policy's 'spend' allowlist and WRONGLY deny a legitimate action."""
    if isinstance(action_class, Enum):
        return str(action_class.value)
    return str(action_class)


# ===========================================================================
# The policy action taxonomy + decision
# ===========================================================================


class PolicyActionClass(str, Enum):
    """The action classes the policy bounds. SUPERSET of the VT-467 business-impact classes — the
    policy ALSO bounds CUSTOMER_SEND (which VT-460, not VT-467, gates downstream). The policy is the
    one place that answers "is this TYPE of action allowed for this tenant at all", across both rails.
    """

    CUSTOMER_SEND = "customer_send"
    SPEND = "spend"
    COMMITMENT = "commitment"
    CONFIG = "config"


class PolicyDecision(str, Enum):
    """The deterministic policy outcome. Exactly two terminal decisions — no third 'maybe'."""

    IN_POLICY = "in_policy"
    OUT_OF_POLICY = "out_of_policy"


# Deterministic OUT_OF_POLICY reason markers (CL-390: a code, never an instruction body).
REASON_NO_POLICY = "no_policy_set"                  # fail-closed default — no grant exists
REASON_ACTION_TYPE_NOT_ALLOWED = "action_type_not_allowed"  # action class not in allowed_action_types
REASON_SEGMENT_NOT_ALLOWED = "segment_not_allowed"  # the action's target segment not allowed
REASON_FREQUENCY_CAP_EXCEEDED = "frequency_cap_exceeded"    # period count >= the owner's freq cap
REASON_SPEND_CEILING_EXCEEDED = "spend_ceiling_exceeded"    # magnitude > the owner's spend ceiling
REASON_MALFORMED_INTENT = "malformed_intent"        # the intent's own fields are unusable → deny
REASON_IN_POLICY = "in_policy"                       # all bounds satisfied


# ===========================================================================
# The policy structure — read off the tenant row, fail-closed-constructed
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BusinessPolicy:
    """The machine-enforceable policy the guard bound-checks against. Constructed from the stored
    JSONB (migration 144) — a MISSING row / empty JSON yields ``_DENY_ALL`` (the most-restrictive
    policy). Every field defaults to its DENY value so a partial/corrupt policy never widens.

      - ``allowed_action_types`` — the action classes the team may take autonomously (the others are
        OUT_OF_POLICY). Empty ⇒ none allowed.
      - ``allowed_segments``     — the customer segments the team may target (for CUSTOMER_SEND).
        Empty ⇒ no segment allowed. The literal ``"all"`` is a wildcard the owner can grant.
      - ``frequency_caps``       — {cap_key: max_in_period}. A MISSING key ⇒ 0 (deny). The caller
        supplies the current period count; the guard compares.
      - ``spend_ceiling_minor``  — the max single-action spend magnitude (paise) the policy permits.
        0 ⇒ deny any spend. (This is the POLICY's hard outer ceiling; the per-class autonomy tier's
        ``autonomous_ceiling_minor`` is the INNER autonomous-vs-approval line beneath it.)
    """

    allowed_action_types: frozenset[str] = field(default_factory=frozenset)
    allowed_segments: frozenset[str] = field(default_factory=frozenset)
    frequency_caps: dict[str, int] = field(default_factory=dict)
    spend_ceiling_minor: int = 0

    def allows_action_type(self, action_class: str) -> bool:
        return action_class in self.allowed_action_types

    def allows_segment(self, segment: str | None) -> bool:
        """A segment is allowed iff explicitly listed OR the wildcard ``"all"`` is granted. A NULL/
        empty segment is only allowed by the wildcard (an action with no segment cannot claim one)."""
        if "all" in self.allowed_segments:
            return True
        if not segment:
            return False
        return segment in self.allowed_segments

    def frequency_cap(self, cap_key: str) -> int:
        """The owner's cap for this key; a MISSING key is 0 (deny). Non-int stored value ⇒ 0."""
        raw = self.frequency_caps.get(cap_key)
        try:
            return max(0, int(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            return 0


# The fail-closed floor: no row → this. Every bound is the DENY value.
_DENY_ALL = BusinessPolicy()


@dataclass(frozen=True, slots=True)
class PolicyCheck:
    """The deterministic policy-check outcome. PII-safe (action class + segment marker + reason CODE
    + numeric bounds only)."""

    decision: PolicyDecision
    reason: str
    action_class: str

    @property
    def in_policy(self) -> bool:
        return self.decision is PolicyDecision.IN_POLICY

    @property
    def out_of_policy(self) -> bool:
        return self.decision is PolicyDecision.OUT_OF_POLICY


_SELECT_POLICY = "SELECT policy FROM tenant_business_policy WHERE tenant_id = %s"


def _row_to_policy(row: Any) -> BusinessPolicy:
    """Build the policy from a psycopg row (dict or tuple). ``None`` row → ``_DENY_ALL``. Every
    malformed field falls back to its DENY value — a corrupt policy can never widen authority."""
    if row is None:
        return _DENY_ALL
    raw = row["policy"] if isinstance(row, dict) else row[0]
    if not isinstance(raw, dict):
        # JSONB usually deserializes to dict; a string (driver edge) is parsed, anything else denies.
        if isinstance(raw, (str, bytes)):
            import json

            try:
                parsed = json.loads(raw)
                raw = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                raw = {}
        else:
            raw = {}

    def _str_set(key: str) -> frozenset[str]:
        v = raw.get(key)
        if not isinstance(v, list):
            return frozenset()
        return frozenset(str(x) for x in v if isinstance(x, (str, int)))

    caps_raw = raw.get("frequency_caps")
    caps: dict[str, int] = {}
    if isinstance(caps_raw, dict):
        for k, val in caps_raw.items():
            try:
                caps[str(k)] = max(0, int(val))
            except (TypeError, ValueError):
                caps[str(k)] = 0  # a malformed cap denies, never widens

    ceiling_raw = raw.get("spend_ceiling_minor")
    try:
        ceiling = max(0, int(ceiling_raw)) if ceiling_raw is not None else 0
    except (TypeError, ValueError):
        ceiling = 0

    return BusinessPolicy(
        allowed_action_types=_str_set("allowed_action_types"),
        allowed_segments=_str_set("allowed_segments"),
        frequency_caps=caps,
        spend_ceiling_minor=ceiling,
    )


def get_business_policy(tenant_id: UUID | str, *, conn: Any = None) -> BusinessPolicy:
    """Read the tenant's machine-enforceable policy.

    FAIL-CLOSED: a MISSING row → ``_DENY_ALL`` (no action type, no segment, every cap 0, ceiling 0).
    A tenant with no explicit owner grant can take NO autonomous business action. ``conn`` is the
    caller's RLS-scoped ``tenant_connection``; RLS independently confirms the tenant.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    if conn is not None:
        row = conn.execute(_SELECT_POLICY, (str(tid),)).fetchone()
    else:
        from orchestrator.db import tenant_connection

        with tenant_connection(tid) as c:
            row = c.execute(_SELECT_POLICY, (str(tid),)).fetchone()
    return _row_to_policy(row)


# ===========================================================================
# THE BOUND-CHECK — the pure deterministic decision (no I/O — the rail's core)
# ===========================================================================


def decide_within_policy(
    policy: BusinessPolicy,
    action_class: PolicyActionClass | str,
    action_attrs: dict[str, Any] | None,
) -> PolicyCheck:
    """The PURE deterministic bound-check (no DB — testable in isolation, the A2 core).

    ``action_attrs`` are the intent's machine-checkable fields (all optional; absent ⇒ the strictest
    interpretation):
      - ``segment``           — the customer segment the action targets (CUSTOMER_SEND). Bound-checked
        against ``allowed_segments``. Absent ⇒ only the ``"all"`` wildcard admits it.
      - ``magnitude_minor``   — the spend magnitude (paise) for SPEND. Bound-checked against
        ``spend_ceiling_minor``. STRICTLY ABOVE the ceiling ⇒ OUT_OF_POLICY (the ceiling is inclusive).
      - ``frequency_cap_key`` + ``period_count`` — the owner's cap to enforce + the current count in
        the period (the CALLER reads the count; the guard compares). count >= cap ⇒ OUT_OF_POLICY.

    The check ladder (fail-closed at every branch; the FIRST violated bound returns its reason):
      1. action TYPE not in allowed_action_types → OUT_OF_POLICY (the brain cannot take a type the
         owner never granted, regardless of magnitude/tier).
      2. CUSTOMER_SEND: the target segment must be allowed.
      3. SPEND: magnitude must be <= the policy spend ceiling (the OUTER bound below the per-class tier).
      4. a declared frequency_cap_key: the current period_count must be strictly BELOW the cap.
    Any branch not satisfied is OUT_OF_POLICY; all satisfied is IN_POLICY.
    """
    ac = _action_class_value(action_class)
    attrs = action_attrs or {}

    def _check(decision: PolicyDecision, reason: str) -> PolicyCheck:
        return PolicyCheck(decision=decision, reason=reason, action_class=ac)

    # 1. The action TYPE must be granted. The brain cannot tier/threshold its way past this.
    if not policy.allows_action_type(ac):
        return _check(PolicyDecision.OUT_OF_POLICY, REASON_ACTION_TYPE_NOT_ALLOWED)

    # 2. Segment bound (customer-send): the targeted segment must be allowed.
    if ac == PolicyActionClass.CUSTOMER_SEND.value:
        segment = attrs.get("segment")
        if not policy.allows_segment(segment if isinstance(segment, str) or segment is None else None):
            return _check(PolicyDecision.OUT_OF_POLICY, REASON_SEGMENT_NOT_ALLOWED)

    # 3. Spend ceiling (spend): the magnitude must be within the policy's OUTER ceiling.
    if ac == PolicyActionClass.SPEND.value and "magnitude_minor" in attrs:
        raw = attrs.get("magnitude_minor")
        try:
            magnitude = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _check(PolicyDecision.OUT_OF_POLICY, REASON_MALFORMED_INTENT)
        # A negative magnitude (refund/credit) is a consequential action the policy never auto-admits.
        if magnitude < 0 or magnitude > policy.spend_ceiling_minor:
            return _check(PolicyDecision.OUT_OF_POLICY, REASON_SPEND_CEILING_EXCEEDED)

    # 4. Frequency cap: when the caller declares a cap key + the current period count, enforce it.
    cap_key = attrs.get("frequency_cap_key")
    if isinstance(cap_key, str) and cap_key:
        cap = policy.frequency_cap(cap_key)
        raw_count = attrs.get("period_count", 0)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return _check(PolicyDecision.OUT_OF_POLICY, REASON_MALFORMED_INTENT)
        # cap 0 (missing key) or count at/over the cap → deny. Strictly-below is the only pass.
        if count >= cap:
            return _check(PolicyDecision.OUT_OF_POLICY, REASON_FREQUENCY_CAP_EXCEEDED)

    return _check(PolicyDecision.IN_POLICY, REASON_IN_POLICY)


def assert_within_policy(
    tenant_id: UUID | str,
    action_class: PolicyActionClass | str,
    action_attrs: dict[str, Any] | None = None,
    *,
    conn: Any = None,
) -> PolicyCheck:
    """THE entry bound-check every action path routes through before its effect.

    DETERMINISTICALLY decides IN_POLICY vs OUT_OF_POLICY from {the tenant's stored policy, the action
    class, the intent's machine-checkable attrs}. Reads ``tenant_business_policy`` (fail-closed: no row
    → OUT_OF_POLICY on everything). NEVER raises for a policy decision — it RETURNS the check; the
    caller acts on it (in-policy ⇒ continue to the rail's own gate; out-of-policy ⇒ gate/escalate to
    the owner, NEVER execute).

    The brain emits the intent; this is the deterministic bound it cannot reason out of.
    """
    policy = get_business_policy(tenant_id, conn=conn)
    check = decide_within_policy(policy, action_class, action_attrs)
    logger.info(
        "business_policy: check tenant=%s class=%s decision=%s reason=%s",
        str(tenant_id), check.action_class, check.decision.value, check.reason,
    )
    return check


# ===========================================================================
# Grant the policy (the explicit owner act — onboarding / an owner approval)
# ===========================================================================


def grant_business_policy(
    tenant_id: UUID | str,
    *,
    allowed_action_types: list[str] | None = None,
    allowed_segments: list[str] | None = None,
    frequency_caps: dict[str, int] | None = None,
    spend_ceiling_minor: int = 0,
    granted_by: UUID | str | None = None,
    conn: Any,
) -> BusinessPolicy:
    """The owner GRANTS / updates the machine-enforceable policy (the onboarding grant or an owner
    approval resolution). DETERMINISTIC: stores the bounds the guard reads. This is an explicit OWNER
    act, never the brain's choice — a specialist can PROPOSE a policy (an owner approval) but only the
    owner's resolution calls this. Upsert on tenant_id. MUST run on the caller's RLS-scoped ``conn``.

    Stores ONLY the recognized fields (an unrecognized key never enters the policy), each normalized
    to its safe form, so a grant can never inject a field the guard does not understand.
    """
    from psycopg.types.json import Jsonb

    tid = str(tenant_id)
    try:
        ceiling = max(0, int(spend_ceiling_minor))
    except (TypeError, ValueError):
        ceiling = 0
    caps: dict[str, int] = {}
    for k, v in (frequency_caps or {}).items():
        try:
            caps[str(k)] = max(0, int(v))
        except (TypeError, ValueError):
            caps[str(k)] = 0
    policy_doc: dict[str, Any] = {
        "allowed_action_types": sorted({str(x) for x in (allowed_action_types or [])}),
        "allowed_segments": sorted({str(x) for x in (allowed_segments or [])}),
        "frequency_caps": caps,
        "spend_ceiling_minor": ceiling,
    }
    grant_id = str(granted_by) if granted_by is not None else None
    conn.execute(
        "INSERT INTO tenant_business_policy (tenant_id, policy, granted_by, granted_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now()) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "policy = EXCLUDED.policy, granted_by = EXCLUDED.granted_by, "
        "granted_at = now(), updated_at = now()",
        (tid, Jsonb(policy_doc), grant_id),
    )
    logger.info(
        "business_policy: granted tenant=%s action_types=%s segments=%s ceiling_minor=%d",
        tid, policy_doc["allowed_action_types"], policy_doc["allowed_segments"], ceiling,
    )
    return get_business_policy(tenant_id, conn=conn)



# ===========================================================================
# PROPOSE + RESOLVE (VT-609 fix round) — the Pillar-7 arm/resolve pattern this module's OWN
# docstring above always promised ("a specialist can PROPOSE a policy... but only the owner's
# resolution calls this") but had ZERO callers until the onboarding-conductor specialist tried to
# call ``grant_business_policy`` DIRECTLY from its own tool-call turn — a Pillar-7 violation (no
# owner-approval provenance, no bounds validation, ``granted_by`` NULL). This mirrors
# ``business_impact_choke``'s decaying-HITL arm/resolve shape (``dispatch_autonomy_offer`` ->
# ``resolve_and_grant_l3``): a tool call ARMS a durable, resolvable ``pending_approvals`` row
# carrying the EXACT proposed bounds; only a SEPARATE resolution call — triggered by the owner's
# own explicit yes/no, recognized by the conversational specialist but never invented by it — reads
# those SAME stored bounds back and calls ``grant_business_policy``, tying the grant to the
# approval-row id as provenance. The model can PROPOSE; it structurally cannot GRANT.
#
# Deliberately does NOT route through ``agent.tools.request_owner_approval.arm_pause_request``
# (the mechanism ``autonomy_upgrade``/``business_impact_action`` use): that primitive's step 1
# unconditionally sends a REGISTERED WhatsApp approval TEMPLATE, and no such template exists (or is
# authorized — ``.viabe/templates.md`` is the canonical registry, hard-coded SIDs are never
# allowed) for a policy-bounds ask. The onboarding-conductor is ALREADY mid-conversation with the
# owner when it proposes bounds — its own composed reply IS the ask; a second templated message
# would be a confusing duplicate. So this arms the durable ``pending_approvals`` row directly
# (same row shape, same one-open-approval-per-tenant structural rule, migration 128's partial
# unique index) without the template send.
# ===========================================================================

APPROVAL_TYPE_POLICY_GRANT = "business_policy_grant"
_PROPOSAL_TIMEOUT_HOURS = 48  # mirrors request_owner_approval._DEFAULT_TIMEOUT_HOURS


def propose_business_policy_grant(
    tenant_id: UUID | str,
    *,
    allowed_action_types: list[str],
    allowed_segments: list[str],
    frequency_caps: dict[str, int],
    spend_ceiling_minor: int,
    conn: Any,
) -> dict[str, Any]:
    """ARM a durable owner-approval row carrying the PROPOSED bounds. The caller (the
    ``propose_business_policy`` tool) has already validated/clamped these — this function trusts
    its own inputs are sane; it does not re-validate business rules, only normalizes shape.

    Refuses (never raises) if another approval is already open for the tenant — the structural
    one-open-approval-per-tenant rule every approval surface in this codebase shares (migration
    128's partial unique index is the race-loser backstop). Mirrors
    ``business_impact_choke.dispatch_autonomy_offer``'s own minimal-provenance-run pattern
    (``pending_approvals.run_id`` FKs ``pipeline_runs`` NOT NULL; the proposal has no agent run of
    its own, so a minimal one is opened to hang the approval off).

    Returns ``{"status": "pending_owner_approval", "approval_id": ..., **the stored bounds}`` on
    success, or ``{"status": "refused", "reason": "approval_queue_busy"}`` when another approval is
    already open (the caller should tell the owner to finish that first)."""
    from datetime import UTC, datetime, timedelta

    from psycopg.errors import UniqueViolation
    from psycopg.types.json import Jsonb

    from orchestrator.db.wrappers import PendingApprovalsWrapper

    tid = str(tenant_id)
    wrapper = PendingApprovalsWrapper()
    if wrapper.has_open_for_tenant(tid, conn=conn):
        return {"status": "refused", "reason": "approval_queue_busy"}

    bounds: dict[str, Any] = {
        "allowed_action_types": sorted({str(x) for x in allowed_action_types}),
        "allowed_segments": sorted({str(x) for x in allowed_segments}),
        "frequency_caps": {str(k): int(v) for k, v in (frequency_caps or {}).items()},
        "spend_ceiling_minor": int(spend_ceiling_minor),
    }

    run_id = uuid4()
    conn.execute(
        "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
        "VALUES (%s, %s, 'business_policy_proposal', 'running') ON CONFLICT (id) DO NOTHING",
        (str(run_id), tid),
    )
    approval_id = uuid4()
    timeout_at = datetime.now(UTC) + timedelta(hours=_PROPOSAL_TIMEOUT_HOURS)
    row: dict[str, Any] = {
        "id": str(approval_id),
        "run_id": str(run_id),
        "approval_type": APPROVAL_TYPE_POLICY_GRANT,
        "summary": "Owner business-policy bounds proposal",
        "details": Jsonb(bounds),
        "status": "pending",
        "decision": None,
        "timeout_at": timeout_at,
    }
    try:
        wrapper.insert(tid, row, conn=conn)
    except UniqueViolation:
        # Race-loser: a concurrent armer won the one-open-per-tenant index between the check above
        # and this INSERT. Same refusal as the pre-check (VT-369 §4.1 shape).
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 — autocommit conn: nothing pending
            pass
        return {"status": "refused", "reason": "approval_queue_busy"}

    logger.info(
        "business_policy: proposal armed tenant=%s approval=%s action_types=%s",
        tid, approval_id, bounds["allowed_action_types"],
    )
    return {"status": "pending_owner_approval", "approval_id": str(approval_id), **bounds}


def resolve_business_policy_grant(
    tenant_id: UUID | str, *, approved: bool, conn: Any
) -> dict[str, Any]:
    """Resolve the tenant's latest OPEN ``business_policy_grant`` approval — the ONLY path that may
    call ``grant_business_policy``. Reads the bounds OFF THE APPROVAL ROW (the exact bounds the
    owner was shown when the proposal was armed) — NEVER a fresh model-supplied value at resolution
    time, so a model recognizing "sure, go ahead" can never smuggle in a broader/different grant
    than what was actually proposed and shown to the owner.

    Idempotent: no open proposal -> a clean no-op (``{"status": "no_pending_proposal"}``) — a
    duplicate/late resolve after the grant already landed (or after the 48h sweep expired it, see
    ``scheduled_triggers.run_approval_timeout_sweep_body``) finds nothing open and does nothing."""
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    tid = str(tenant_id)
    wrapper = PendingApprovalsWrapper()
    open_row = wrapper.latest_open_of_type(tid, APPROVAL_TYPE_POLICY_GRANT, conn=conn)
    if open_row is None:
        return {"status": "no_pending_proposal"}

    approval_id = open_row["id"]
    details = open_row.get("details") or {}
    if not isinstance(details, dict):
        import json as _json

        try:
            details = _json.loads(details)
        except (TypeError, ValueError):
            details = {}

    decision = "approved" if approved else "rejected"
    wrapper.mark_resolved(tid, approval_id, decision=decision, status=decision, conn=conn)

    if not approved:
        logger.info("business_policy: proposal rejected tenant=%s approval=%s", tid, approval_id)
        return {"status": "rejected", "approval_id": approval_id}

    policy = grant_business_policy(
        tid,
        allowed_action_types=list(details.get("allowed_action_types") or []),
        allowed_segments=list(details.get("allowed_segments") or []),
        frequency_caps=dict(details.get("frequency_caps") or {}),
        spend_ceiling_minor=int(details.get("spend_ceiling_minor") or 0),
        granted_by=approval_id,
        conn=conn,
    )
    logger.info("business_policy: proposal approved+granted tenant=%s approval=%s", tid, approval_id)
    return {
        "status": "granted",
        "approval_id": approval_id,
        "allowed_action_types": sorted(policy.allowed_action_types),
        "allowed_segments": sorted(policy.allowed_segments),
        "frequency_caps": dict(policy.frequency_caps),
        "spend_ceiling_minor": policy.spend_ceiling_minor,
    }


__all__ = [
    "PolicyActionClass",
    "PolicyDecision",
    "BusinessPolicy",
    "PolicyCheck",
    "REASON_NO_POLICY",
    "REASON_ACTION_TYPE_NOT_ALLOWED",
    "REASON_SEGMENT_NOT_ALLOWED",
    "REASON_FREQUENCY_CAP_EXCEEDED",
    "REASON_SPEND_CEILING_EXCEEDED",
    "REASON_MALFORMED_INTENT",
    "REASON_IN_POLICY",
    "get_business_policy",
    "decide_within_policy",
    "assert_within_policy",
    "grant_business_policy",
    "APPROVAL_TYPE_POLICY_GRANT",
    "propose_business_policy_grant",
    "resolve_business_policy_grant",
]
