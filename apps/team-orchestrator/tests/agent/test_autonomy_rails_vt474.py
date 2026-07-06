"""VT-474 — adversarial proof for the autonomy rails the six lanes depend on (extends VT-460/467 set D).

Three parts, each proven against the REAL deterministic code (no LLM anywhere — the rails are
deterministic by design):

  A2 — POLICY bound-check: "within policy" is a machine-enforceable bound, NOT the brain's judgment.
       An out-of-policy action is gated/escalated, never executed; the brain cannot reason out of it.
  A3 — ESCALATION triggers: a pure decision over CONCRETE triggers; each fires deterministically.
  B  — SEND decaying-checkpoint: first-send → checkpoint, proven tenant → autonomous (reusing the
       EXISTING VTR/L2-L3 decay + the is_always_confirm floor; NO new decay model).

Layers A/B/C are PURE (no DB) — the deterministic cores. Layer D is DB-backed end-to-end through the
real gate/choke + RLS (gated on DATABASE_URL), mirroring test_business_impact_rails_nonbypassability.py.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

# ===========================================================================
# A2 — POLICY bound-check (pure, no DB): the deterministic decision
# ===========================================================================

from orchestrator.agents.business_policy import (  # noqa: E402
    REASON_ACTION_TYPE_NOT_ALLOWED,
    REASON_FREQUENCY_CAP_EXCEEDED,
    REASON_MALFORMED_INTENT,
    REASON_SEGMENT_NOT_ALLOWED,
    REASON_SPEND_CEILING_EXCEEDED,
    BusinessPolicy,
    PolicyActionClass,
    _DENY_ALL,
    _row_to_policy,
    decide_within_policy,
)


def _policy(**kw: object) -> BusinessPolicy:
    return _row_to_policy({"policy": dict(kw)})


def test_A2_fail_closed_default_denies_everything() -> None:
    """A2 — THE fail-closed default. A MISSING policy row (``_DENY_ALL``) is OUT_OF_POLICY for every
    action type — a tenant with no explicit owner grant can take NO autonomous business action."""
    for ac in PolicyActionClass:
        check = decide_within_policy(_DENY_ALL, ac, {"magnitude_minor": 1, "segment": "all"})
        assert check.out_of_policy
        assert check.reason == REASON_ACTION_TYPE_NOT_ALLOWED


def test_A2_action_type_must_be_granted() -> None:
    """A2 — the brain cannot take an action TYPE the owner never granted (the first bound). A policy
    granting only 'spend' denies 'config'/'commitment'/'customer_send'."""
    p = _policy(allowed_action_types=["spend"], spend_ceiling_minor=10_000)
    assert decide_within_policy(p, PolicyActionClass.SPEND, {"magnitude_minor": 1}).in_policy
    for ac in (PolicyActionClass.CONFIG, PolicyActionClass.COMMITMENT, PolicyActionClass.CUSTOMER_SEND):
        assert decide_within_policy(p, ac, {}).reason == REASON_ACTION_TYPE_NOT_ALLOWED


def test_A2_spend_ceiling_is_inclusive_boundary() -> None:
    """A2 — spend ceiling: at/below the ceiling is in policy, strictly above is out (the OUTER bound
    the per-class tier sits beneath). A negative magnitude (refund) is never auto-in-policy."""
    p = _policy(allowed_action_types=["spend"], spend_ceiling_minor=50_000)
    assert decide_within_policy(p, PolicyActionClass.SPEND, {"magnitude_minor": 50_000}).in_policy
    above = decide_within_policy(p, PolicyActionClass.SPEND, {"magnitude_minor": 50_001})
    assert above.out_of_policy and above.reason == REASON_SPEND_CEILING_EXCEEDED
    neg = decide_within_policy(p, PolicyActionClass.SPEND, {"magnitude_minor": -1})
    assert neg.out_of_policy and neg.reason == REASON_SPEND_CEILING_EXCEEDED


def test_A2_segment_bound_for_customer_send() -> None:
    """A2 — the targeted segment must be allowed; the 'all' wildcard admits any segment; an
    un-granted segment is out of policy. The brain cannot target a segment the owner never granted."""
    p = _policy(allowed_action_types=["customer_send"], allowed_segments=["lapsed"])
    assert decide_within_policy(p, PolicyActionClass.CUSTOMER_SEND, {"segment": "lapsed"}).in_policy
    bad = decide_within_policy(p, PolicyActionClass.CUSTOMER_SEND, {"segment": "vip"})
    assert bad.out_of_policy and bad.reason == REASON_SEGMENT_NOT_ALLOWED
    # no segment + no wildcard → denied
    assert decide_within_policy(p, PolicyActionClass.CUSTOMER_SEND, {"segment": None}).out_of_policy
    # wildcard admits anything
    pw = _policy(allowed_action_types=["customer_send"], allowed_segments=["all"])
    assert decide_within_policy(pw, PolicyActionClass.CUSTOMER_SEND, {"segment": "anything"}).in_policy


def test_A2_frequency_cap_strictly_below() -> None:
    """A2 — a declared frequency cap: the period count must be STRICTLY below the cap; at/over → out.
    A missing cap key is 0 (deny)."""
    p = _policy(allowed_action_types=["customer_send"], allowed_segments=["all"],
                frequency_caps={"send_per_day": 5})
    ok = decide_within_policy(p, PolicyActionClass.CUSTOMER_SEND,
                              {"segment": "all", "frequency_cap_key": "send_per_day", "period_count": 4})
    assert ok.in_policy
    at = decide_within_policy(p, PolicyActionClass.CUSTOMER_SEND,
                              {"segment": "all", "frequency_cap_key": "send_per_day", "period_count": 5})
    assert at.out_of_policy and at.reason == REASON_FREQUENCY_CAP_EXCEEDED
    # an undeclared cap key is 0 → any count denies
    miss = decide_within_policy(p, PolicyActionClass.CUSTOMER_SEND,
                                {"segment": "all", "frequency_cap_key": "unknown", "period_count": 0})
    assert miss.out_of_policy and miss.reason == REASON_FREQUENCY_CAP_EXCEEDED


def test_A2_malformed_policy_and_intent_fail_closed() -> None:
    """A2 — a corrupt stored policy never WIDENS authority (every malformed field → its deny value),
    and a malformed intent magnitude/count is denied (never silently passed)."""
    # garbage policy → deny-all
    assert _row_to_policy({"policy": "garbage"}).allowed_action_types == frozenset()
    assert _row_to_policy({"policy": {"allowed_action_types": "spend"}}).allowed_action_types == frozenset()
    assert _row_to_policy({"policy": {"spend_ceiling_minor": "lots"}}).spend_ceiling_minor == 0
    # malformed intent magnitude
    p = _policy(allowed_action_types=["spend"], spend_ceiling_minor=10_000)
    bad = decide_within_policy(p, PolicyActionClass.SPEND, {"magnitude_minor": "abc"})
    assert bad.out_of_policy and bad.reason == REASON_MALFORMED_INTENT


# ===========================================================================
# A3 — ESCALATION (pure, no DB): each trigger fires deterministically
# ===========================================================================

from orchestrator.agents.escalation import (  # noqa: E402
    EscalationReason,
    should_escalate,
)

_T = UUID(int=7)


def test_A3_nothing_triggers_steady_state() -> None:
    """A3 — steady-state autonomy: an empty / below-threshold context does NOT escalate (the owner is
    not pestered). This is the §6 default — the team runs the business without owner-in-the-loop."""
    assert should_escalate(_T, {}).reason is None
    assert should_escalate(_T, {"complaint_count": 1, "opt_out_count": 2, "rail_trip_count": 2,
                                "specialist_failure_count": 2}).reason is None


@pytest.mark.parametrize(
    "ctx,expected",
    [
        ({"money_movement_request": True}, EscalationReason.MONEY_MOVEMENT_REQUEST),
        ({"out_of_policy_irreversible": True}, EscalationReason.OUT_OF_POLICY_IRREVERSIBLE),
        ({"complaint_count": 2}, EscalationReason.COMPLAINT_SURGE),
        ({"opt_out_count": 3}, EscalationReason.OPT_OUT_SURGE),
        ({"rail_trip_count": 3}, EscalationReason.REPEATED_RAIL_TRIP),
        ({"specialist_failure_count": 3}, EscalationReason.REPEATED_SPECIALIST_FAILURE),
        ({"spend_window_minor": 1_000, "spend_baseline_minor": 100}, EscalationReason.SPEND_ANOMALY),
        ({"volume_window": 40, "volume_baseline": 10}, EscalationReason.VOLUME_ANOMALY),
        ({"send_quality_flag": True}, EscalationReason.SEND_QUALITY_FLAG),
    ],
)
def test_A3_each_trigger_fires(ctx: dict, expected: EscalationReason) -> None:
    """A3 — every concrete trigger fires its reason deterministically. These are the §8 extreme-
    scenario triggers; the decision is over machine-checkable inputs, never the brain's vibe."""
    assert should_escalate(_T, ctx).reason == expected


def test_A3_money_movement_always_escalates_first() -> None:
    """A3 — money-movement / return-filing is ALWAYS escalated (never autonomous in v1) and wins over
    a co-present lower trigger (it is checked first as the highest-stakes scenario)."""
    d = should_escalate(_T, {"money_movement_request": True, "complaint_count": 2})
    assert d.reason == EscalationReason.MONEY_MOVEMENT_REQUEST


def test_A3_anomaly_needs_a_baseline() -> None:
    """A3 — a spend/volume anomaly needs a baseline to be a multiple OF; a cold-start (0 baseline)
    does NOT fire the anomaly trigger (it routes through the A2 spend-ceiling rail instead) — so the
    anomaly trigger never fires on every first-ever spend."""
    assert should_escalate(_T, {"spend_window_minor": 10**9, "spend_baseline_minor": 0}).reason is None
    # just under 3x is not an anomaly; at/over 3x is
    assert should_escalate(_T, {"spend_window_minor": 299, "spend_baseline_minor": 100}).reason is None
    assert should_escalate(_T, {"spend_window_minor": 301, "spend_baseline_minor": 100}).reason \
        == EscalationReason.SPEND_ANOMALY


def test_A3_escalate_owner_noop_when_nothing_to_escalate() -> None:
    """A3 — the owner-notify seam is a no-op when the decision did not trigger (no spurious owner
    pings). Uses an injected sender that would record any call."""
    from orchestrator.agents.escalation import EscalationDecision, escalate_owner

    calls: list = []

    def _sender(tid, params):  # type: ignore[no-untyped-def]
        calls.append((tid, params))
        return SimpleNamespace(success=True, message_sid="SM_should_not_send")

    sid = escalate_owner(_T, EscalationDecision(reason=None), send_fn=_sender)
    assert sid is None and calls == []


# ===========================================================================
# B — SEND decaying-checkpoint (pure decision shape): reuses the existing decay
# ===========================================================================

from orchestrator.agents.send_checkpoint import (  # noqa: E402
    REASON_ALWAYS_CONFIRM_FLOOR,
    REASON_FROZEN,
    REASON_L2_NOT_PROVEN,
    REASON_L3_AUTONOMOUS,
    SendAutonomyDecision,
    SendCheckpointResult,
)


def test_B_decision_shape_two_terminal_states() -> None:
    """B — the decision is exactly two terminal states (checkpoint | autonomous) with a reason code —
    the same shape discipline as the other rails. The DB-backed proof (layer D) exercises the curve
    against the real decay; this pins the value object."""
    cp = SendCheckpointResult(decision=SendAutonomyDecision.CHECKPOINT, reason=REASON_L2_NOT_PROVEN, level="L2")
    assert cp.checkpoint and not cp.autonomous
    au = SendCheckpointResult(decision=SendAutonomyDecision.AUTONOMOUS, reason=REASON_L3_AUTONOMOUS, level="L3")
    assert au.autonomous and not au.checkpoint


# ===========================================================================
# Layer D — DB-BACKED end-to-end through the REAL gates + RLS
# ===========================================================================

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards

from orchestrator.agents import business_impact_choke as choke  # noqa: E402
from orchestrator.agents import business_policy as bp  # noqa: E402
from orchestrator.agents.business_impact_sample import propose_spend  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-474 DB-backed proof tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS (mirrors the VT-467 proof)."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt474-railproof-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, business_type, "
            "verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', 'gstin_verified', %s) "
            "RETURNING id",
            ("VT-474 railproof", f"+9197{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _run_id(dsn: str, tenant: UUID) -> UUID:
    rid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'business_impact', 'running')",
            (str(rid), str(tenant)),
        )
    return rid


@requires_db
def test_D_policy_missing_row_is_deny_all(substrate) -> None:  # type: ignore[no-untyped-def]
    """D — fail-closed default through real RLS: a tenant with NO policy row reads ``_DENY_ALL`` and
    every action class is OUT_OF_POLICY."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        policy = bp.get_business_policy(tenant, conn=conn)
        assert policy.allowed_action_types == frozenset()
        check = bp.assert_within_policy(
            tenant, bp.PolicyActionClass.SPEND, {"magnitude_minor": 1}, conn=conn
        )
        assert check.out_of_policy


@requires_db
def test_D_grant_then_in_policy(substrate) -> None:  # type: ignore[no-untyped-def]
    """D — the owner grant (the decay/loosen) is read back through RLS: after grant_business_policy a
    within-bounds action is IN_POLICY and an out-of-bounds one is OUT_OF_POLICY."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        bp.grant_business_policy(
            tenant, allowed_action_types=["spend"], spend_ceiling_minor=50_000, conn=conn
        )
    with tenant_connection(tenant) as conn:
        assert bp.assert_within_policy(
            tenant, bp.PolicyActionClass.SPEND, {"magnitude_minor": 49_999}, conn=conn
        ).in_policy
        assert bp.assert_within_policy(
            tenant, bp.PolicyActionClass.SPEND, {"magnitude_minor": 50_001}, conn=conn
        ).out_of_policy


@requires_db
def test_D_out_of_policy_forces_owner_approval_regardless_of_tier(substrate) -> None:  # type: ignore[no-untyped-def]
    """D — THE A2 non-bypassability proof: a tenant with a GENEROUS per-class autonomy tier (so the
    tier alone would say AUTONOMOUS) but an out-of-policy magnitude is FORCED to owner approval — the
    brain cannot tier its way past the policy. propose_spend(enforce_policy=True) routes to approval
    and the effect does NOT run."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        # A generous tier: a huge auto-approve threshold (tier alone → autonomous for any sane amount).
        choke.grant_business_autonomy(
            tenant, choke.BusinessImpactClass.SPEND, tier=choke.TIER_THRESHOLD,
            auto_approve_below_minor=10**12, conn=conn,
        )
        # Policy allows spend but only up to ₹100 (10_000 paise).
        bp.grant_business_policy(
            tenant, allowed_action_types=["spend"], spend_ceiling_minor=10_000, conn=conn
        )

    # An amount the TIER would auto-approve (< 10^12) but POLICY forbids (> 10_000): owner approval.
    with tenant_connection(tenant) as conn:
        outcome = propose_spend(
            tenant, _run_id(substrate.dsn, tenant), 50_000,
            enforce_policy=True, conn=conn, dry_run=True,
        )
    assert outcome.decision == choke.BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value
    assert outcome.executed is False
    assert outcome.reason.startswith("out_of_policy:")
    assert outcome.approval_status == "armed"  # routed through the EXISTING owner-approval machinery

    # A within-policy + within-tier amount runs autonomously (the rail does not block legitimate work).
    with tenant_connection(tenant) as conn:
        ok = propose_spend(
            tenant, _run_id(substrate.dsn, tenant), 9_999,
            enforce_policy=True, conn=conn, dry_run=True,
        )
    assert ok.decision == choke.BusinessActionDecision.AUTONOMOUS.value and ok.executed is True


@requires_db
def test_D_action_type_not_granted_forces_approval(substrate) -> None:  # type: ignore[no-untyped-def]
    """D — a spend when the policy never granted 'spend' as an allowed action type is forced to owner
    approval even with a permissive autonomy tier (the action-type bound, the first policy gate)."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        choke.grant_business_autonomy(
            tenant, choke.BusinessImpactClass.SPEND, tier=choke.TIER_AUTONOMOUS,
            autonomous_ceiling_minor=None, conn=conn,  # unbounded tier
        )
        # Policy grants ONLY customer_send — spend is not an allowed action type.
        bp.grant_business_policy(
            tenant, allowed_action_types=["customer_send"], allowed_segments=["all"], conn=conn
        )
    with tenant_connection(tenant) as conn:
        outcome = propose_spend(
            tenant, _run_id(substrate.dsn, tenant), 1,
            enforce_policy=True, conn=conn, dry_run=True,
        )
    assert outcome.decision == choke.BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value
    assert outcome.executed is False
    assert "action_type_not_allowed" in outcome.reason


# --- B: the SEND decaying-checkpoint against the REAL decay (autonomy + is_always_confirm) ---

from orchestrator.agents.send_checkpoint import send_checkpoint_decision  # noqa: E402


def _seed_send_substrate(dsn: str, tenant: UUID, agent: str) -> tuple[str, str]:
    """Seed a customer + a prior agent_customer_contacts row for a NON-first-contact, NON-novel
    template send, so the is_always_confirm floor does NOT trip for a proven (L3) tenant. Returns
    (customer_id, template_name)."""
    template_name = "team_winback_simple"
    with psycopg.connect(dsn, autocommit=True) as conn:
        cust = conn.execute(
            "INSERT INTO customers (tenant_id, opt_out_status) VALUES (%s, 'subscribed') RETURNING id",
            (str(tenant),),
        ).fetchone()
        cid = str(cust[0])
        # a prior contact with the SAME template + customer → not first-contact, not novel-template,
        # but > 30d ago so the prior contact itself is not what we are checking here (the floor reads
        # existence of any contact row for first-contact + template existence for novel).
        conn.execute(
            "INSERT INTO agent_customer_contacts (tenant_id, customer_id, agent, template_name, "
            "autonomy_level, sent_at) VALUES (%s, %s, %s, %s, 'L2', now() - interval '120 days')",
            (str(tenant), cid, agent, template_name),
        )
    return cid, template_name


@requires_db
def test_D_send_checkpoint_first_send_is_checkpoint(substrate) -> None:  # type: ignore[no-untyped-def]
    """B/D — a NEW tenant (no autonomy row = L2, un-proven) → the send CHECKPOINTS (owner-visible):
    the first sends per new tenant are owner-visible (the un-decayed leg)."""
    tenant = _new_tenant(substrate.dsn)
    agent = "sales_recovery"
    cid, template = _seed_send_substrate(substrate.dsn, tenant, agent)
    with tenant_connection(tenant) as conn:
        d = send_checkpoint_decision(
            tenant, agent=agent, batch_customer_ids=[cid], template_name=template,
            money_bearing=False, conn=conn,
        )
    assert d.checkpoint and d.reason == REASON_L2_NOT_PROVEN and d.level == "L2"


@requires_db
def test_D_send_checkpoint_proven_tenant_is_autonomous(substrate) -> None:  # type: ignore[no-untyped-def]
    """B/D — a PROVEN tenant (L3, earned) + a non-floor batch (an existing customer, a known template,
    small, non-money) → AUTONOMOUS: decayed to full autonomy once proven safe (the design's curve)."""
    from orchestrator.agents import autonomy

    tenant = _new_tenant(substrate.dsn)
    agent = "sales_recovery"
    cid, template = _seed_send_substrate(substrate.dsn, tenant, agent)
    # Earn L3: a clean streak to threshold, then grant (the explicit owner opt-in evidence).
    with tenant_connection(tenant) as conn:
        for _ in range(autonomy.L3_CLEAN_STREAK_THRESHOLD):
            autonomy.record_approval_outcome(tenant, agent, clean=True, conn=conn)
        autonomy.grant_l3(tenant, agent, uuid4(), conn=conn)
        state = autonomy.get_autonomy(tenant, agent, conn=conn)
        assert state.level == "L3", state

        d = send_checkpoint_decision(
            tenant, agent=agent, batch_customer_ids=[cid], template_name=template,
            money_bearing=False, conn=conn,
        )
    assert d.autonomous and d.reason == REASON_L3_AUTONOMOUS and d.level == "L3"


@requires_db
def test_D_send_checkpoint_l3_money_floor_back_to_checkpoint(substrate) -> None:  # type: ignore[no-untyped-def]
    """B/D — a PROVEN (L3) tenant but a MONEY-bearing template trips the is_always_confirm floor →
    back to CHECKPOINT. The campaign earns its own trust: even a proven tenant checkpoints a money
    send (the non-bypassable CL-438 floor — proves it is NOT per-send-forever, but also NOT a blanket
    autonomy that skips the high-risk first send of a campaign)."""
    from orchestrator.agents import autonomy

    tenant = _new_tenant(substrate.dsn)
    agent = "sales_recovery"
    cid, template = _seed_send_substrate(substrate.dsn, tenant, agent)
    with tenant_connection(tenant) as conn:
        for _ in range(autonomy.L3_CLEAN_STREAK_THRESHOLD):
            autonomy.record_approval_outcome(tenant, agent, clean=True, conn=conn)
        autonomy.grant_l3(tenant, agent, uuid4(), conn=conn)
        d = send_checkpoint_decision(
            tenant, agent=agent, batch_customer_ids=[cid], template_name=template,
            money_bearing=True, conn=conn,  # money trips the floor
        )
    assert d.checkpoint and d.reason == REASON_ALWAYS_CONFIRM_FLOOR and d.floor_reason == "money_template"


@requires_db
def test_D_send_checkpoint_frozen_is_checkpoint(substrate) -> None:  # type: ignore[no-untyped-def]
    """B/D — a FROZEN agent (the kill switch / regression tighten) → CHECKPOINT regardless of any
    earned tier: the decay is two-way; a regression tightens autonomy back to owner-visible."""
    from orchestrator.agents import autonomy

    tenant = _new_tenant(substrate.dsn)
    agent = "sales_recovery"
    cid, template = _seed_send_substrate(substrate.dsn, tenant, agent)
    with tenant_connection(tenant) as conn:
        autonomy.set_frozen(tenant, agent, True, reason="owner_kill", conn=conn)
        d = send_checkpoint_decision(
            tenant, agent=agent, batch_customer_ids=[cid], template_name=template,
            money_bearing=False, conn=conn,
        )
    assert d.checkpoint and d.reason == REASON_FROZEN


# ===========================================================================
# E — VT-609 fix round: the PROPOSE/RESOLVE arm-and-grant shape (CRITICAL — Pillar-7). The
# onboarding-conductor's ``propose_business_policy`` tool must NEVER call ``grant_business_policy``
# directly; only ``resolve_business_policy_grant``, reading the bounds off the durable approval
# row, may. DB-backed end-to-end (RLS-real) — mirrors the D-layer's own real-gate proof shape.
# ===========================================================================


@requires_db
def test_E_propose_arms_a_durable_row_and_does_not_grant(substrate) -> None:  # type: ignore[no-untyped-def]
    """E — ``propose_business_policy_grant`` ARMS a durable ``pending_approvals`` row carrying the
    proposed bounds and does NOT touch ``tenant_business_policy`` at all — the deny-all default
    stands until an explicit resolve."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        result = bp.propose_business_policy_grant(
            tenant,
            allowed_action_types=["customer_send"],
            allowed_segments=["lapsed"],
            frequency_caps={"customer_send_per_month": 2},
            spend_ceiling_minor=50_000,
            conn=conn,
        )
        assert result["status"] == "pending_owner_approval"
        assert result["allowed_action_types"] == ["customer_send"]
        # Deny-all still stands — proposing is not granting.
        policy = bp.get_business_policy(tenant, conn=conn)
        assert policy.allowed_action_types == frozenset()

        row = conn.execute(
            "SELECT approval_type, status, decision FROM pending_approvals "
            "WHERE id = %s", (result["approval_id"],),
        ).fetchone()
        assert row is not None
        assert row["approval_type"] == bp.APPROVAL_TYPE_POLICY_GRANT
        assert row["status"] == "pending"
        assert row["decision"] is None


@requires_db
def test_E_resolve_approved_grants_exactly_the_proposed_bounds_with_provenance(substrate) -> None:  # type: ignore[no-untyped-def]
    """E — THE Pillar-7 provenance proof: resolving ``approved=True`` grants EXACTLY the bounds
    that were proposed (never a fresh value), and ``granted_by`` is the approval-row id — the audit
    trail the direct-grant design had none of."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        proposed = bp.propose_business_policy_grant(
            tenant,
            allowed_action_types=["customer_send", "spend"],
            allowed_segments=["lapsed"],
            frequency_caps={"customer_send_per_month": 2},
            spend_ceiling_minor=50_000,
            conn=conn,
        )
        approval_id = proposed["approval_id"]
        result = bp.resolve_business_policy_grant(tenant, approved=True, conn=conn)

        assert result["status"] == "granted"
        assert result["allowed_action_types"] == ["customer_send", "spend"]
        assert result["spend_ceiling_minor"] == 50_000

        policy = bp.get_business_policy(tenant, conn=conn)
        assert policy.allowed_action_types == frozenset({"customer_send", "spend"})
        assert bp.assert_within_policy(
            tenant, bp.PolicyActionClass.SPEND, {"magnitude_minor": 50_000}, conn=conn
        ).in_policy

        row = conn.execute(
            "SELECT granted_by FROM tenant_business_policy WHERE tenant_id = %s", (str(tenant),),
        ).fetchone()
        assert row is not None
        assert str(row["granted_by"]) == str(approval_id)

        # The approval row itself is now resolved.
        arow = conn.execute(
            "SELECT status, decision, resolved_at FROM pending_approvals WHERE id = %s",
            (approval_id,),
        ).fetchone()
        assert arow is not None
        assert arow["status"] == "approved"
        assert arow["decision"] == "approved"
        assert arow["resolved_at"] is not None


@requires_db
def test_E_resolve_rejected_does_not_grant(substrate) -> None:  # type: ignore[no-untyped-def]
    """E — a rejected resolution leaves the deny-all default in force + marks the row rejected."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        bp.propose_business_policy_grant(
            tenant,
            allowed_action_types=["customer_send"],
            allowed_segments=["lapsed"],
            frequency_caps={},
            spend_ceiling_minor=0,
            conn=conn,
        )
        result = bp.resolve_business_policy_grant(tenant, approved=False, conn=conn)
        assert result["status"] == "rejected"

        policy = bp.get_business_policy(tenant, conn=conn)
        assert policy.allowed_action_types == frozenset()


@requires_db
def test_E_resolve_with_nothing_open_is_a_clean_noop(substrate) -> None:  # type: ignore[no-untyped-def]
    """E — idempotency: a resolve with no open proposal (never proposed, or already resolved, or
    swept as timed-out) is a clean no-op — never a raise, never a phantom grant."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        result = bp.resolve_business_policy_grant(tenant, approved=True, conn=conn)
        assert result == {"status": "no_pending_proposal"}


@requires_db
def test_E_propose_refuses_when_another_approval_is_already_open(substrate) -> None:  # type: ignore[no-untyped-def]
    """E — the structural one-open-approval-per-tenant rule (migration 128) binds the policy
    proposal too: a second propose while the first is still unresolved is refused, never a second
    live row."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        first = bp.propose_business_policy_grant(
            tenant,
            allowed_action_types=["customer_send"],
            allowed_segments=["lapsed"],
            frequency_caps={},
            spend_ceiling_minor=0,
            conn=conn,
        )
        assert first["status"] == "pending_owner_approval"

        second = bp.propose_business_policy_grant(
            tenant,
            allowed_action_types=["spend"],
            allowed_segments=["all"],
            frequency_caps={},
            spend_ceiling_minor=100,
            conn=conn,
        )
        assert second == {"status": "refused", "reason": "approval_queue_busy"}
