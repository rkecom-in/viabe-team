"""VT-467 — adversarial NON-BYPASSABILITY proof for the business-impact rails (extends VT-460 set D).

VT-460's set D proved the CUSTOMER-SEND rail cannot be bypassed. This is the same proof discipline
for the CONSEQUENTIAL business-impact actions (SPEND / COMMITMENT / CONFIG) VT-467 adds — every test
exercises the REAL gate/choke code and proves a consequential side effect is structurally impossible
without {a permitting tier + below-threshold magnitude} OR owner approval. It maps to test-matrix
set D (the deterministic safety rails that must stay 100%) + the autonomy model.

Three layers, mirroring the framework's three structural boundaries:

  A. THE GATE (pure, no DB) — the deterministic autonomous-vs-approval decision.
       D-BIZ-1  fail-closed default: NO autonomy setting → REQUIRES_OWNER_APPROVAL on everything.
       D-BIZ-2  threshold tier: autonomous strictly BELOW the threshold; at/above → approval.
       D-BIZ-3  autonomous tier: autonomous up to the ceiling; above → approval (extreme line).
       D-BIZ-4  frozen → REQUIRES_OWNER_APPROVAL regardless of tier (kill switch wins).
       D-BIZ-5  negative magnitude + unknown tier → fail closed.

  B. THE TRANSPORT CHOKE (no DB) — the effect boundary fails closed for an un-gated action.
       D-BIZ-6  the effect OUTSIDE business_action_context raises UngatedBusinessActionError.
       D-BIZ-7  the effect INSIDE the context is admitted.

  C. THE CAPABILITY GUARD (langchain) — the brain holds NO spend/commit/config-write tool.
       D-BIZ-8  handing the agent builder a spend/commit/config-write tool RAISES at build.

  D. DB-BACKED end-to-end through the SAMPLE action (real tenant_business_autonomy, real RLS):
       D-BIZ-9   no grant (fail-closed) → propose_spend routes to owner approval, effect NOT run.
       D-BIZ-10  granted threshold → below = autonomous (effect runs); at/above = approval.
       D-BIZ-11  granted autonomous + ceiling → within = autonomous; above = approval.
       D-BIZ-12  frozen class → approval even below a previously-granted threshold.

DB substrate mirrors tests/agent/test_rail_harness_nonbypassability.py (importorskip psycopg+dbos,
skipif no DATABASE_URL, migrations applied once + DBOS launched module-scoped, rows seeded via a
service-role connection, the code exercised through tenant_connection — the real RLS path). The
owner-approval arm uses dry_run (no live Twilio).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from orchestrator.agents.business_impact_choke import (
    REASON_ABOVE_CEILING,
    REASON_ALWAYS_APPROVE_TIER,
    REASON_AT_OR_ABOVE_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_FROZEN,
    REASON_NEGATIVE_MAGNITUDE,
    REASON_NO_AUTONOMY_SETTING,
    REASON_WITHIN_CEILING,
    TIER_ALWAYS_APPROVE,
    TIER_AUTONOMOUS,
    TIER_THRESHOLD,
    BusinessActionDecision,
    BusinessAutonomyState,
    BusinessImpactClass,
    decide_business_action,
)

# ===========================================================================
# Layer A — THE GATE (pure, no DB): the deterministic decision
# ===========================================================================


def _state(tier: str, *, below: int | None = None, ceil: int | None = None, frozen: bool = False) -> BusinessAutonomyState:
    return BusinessAutonomyState(
        tenant_id=UUID(int=1), action_class="spend", tier=tier,
        auto_approve_below_minor=below, autonomous_ceiling_minor=ceil, frozen=frozen,
    )


def test_D_BIZ_1_fail_closed_default_requires_approval_for_everything() -> None:
    """D-BIZ-1 — THE fail-closed default. The always_approve tier (= a MISSING row, the constructed
    default) requires owner approval for ANY magnitude, including 0. No grant = no autonomy."""
    state = _state(TIER_ALWAYS_APPROVE)  # what get_business_autonomy returns for a missing row
    for magnitude in (0, 1, 100, 10**9):
        gate = decide_business_action(state, magnitude)
        assert gate.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL
        assert gate.reason == REASON_ALWAYS_APPROVE_TIER
        assert gate.requires_owner_approval and not gate.autonomous


def test_D_BIZ_1b_default_constructed_state_is_always_approve() -> None:
    """D-BIZ-1b — the DEFAULT-constructed state (no kwargs) is the fail-closed floor: a missing
    DB row maps to exactly this, so a tenant with no setting can never act autonomously."""
    default = BusinessAutonomyState(tenant_id=UUID(int=1), action_class="spend")
    assert default.tier == TIER_ALWAYS_APPROVE and default.frozen is False
    assert decide_business_action(default, 1).decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL


def test_D_BIZ_2_threshold_tier_autonomous_strictly_below() -> None:
    """D-BIZ-2 — threshold tier: autonomous STRICTLY below the threshold; AT the threshold and above
    → owner approval (the boundary is closed at the threshold, fail-safe)."""
    state = _state(TIER_THRESHOLD, below=50_000)  # ₹500.00 in paise
    # below → autonomous
    g_below = decide_business_action(state, 49_999)
    assert g_below.decision is BusinessActionDecision.AUTONOMOUS
    assert g_below.reason == REASON_BELOW_THRESHOLD
    # exactly AT → approval (boundary closed)
    g_at = decide_business_action(state, 50_000)
    assert g_at.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL
    assert g_at.reason == REASON_AT_OR_ABOVE_THRESHOLD
    # above → approval
    assert decide_business_action(state, 50_001).requires_owner_approval


def test_D_BIZ_2b_threshold_tier_with_null_threshold_is_autonomous_nowhere() -> None:
    """D-BIZ-2b — threshold tier with a NULL/absent threshold is autonomous NOWHERE (fail-closed):
    a tier without a number can never auto-pass — even magnitude 0 needs approval."""
    state = _state(TIER_THRESHOLD, below=None)
    for magnitude in (0, 1, 100):
        assert decide_business_action(state, magnitude).requires_owner_approval


def test_D_BIZ_3_autonomous_tier_within_ceiling_then_escalates() -> None:
    """D-BIZ-3 — autonomous tier: autonomous up to AND INCLUDING the ceiling; strictly ABOVE the
    ceiling → owner approval (the 'extreme scenario' escalation line, §6)."""
    state = _state(TIER_AUTONOMOUS, ceil=200_000)  # ₹2000 ceiling
    g_within = decide_business_action(state, 200_000)  # exactly at the ceiling → autonomous
    assert g_within.decision is BusinessActionDecision.AUTONOMOUS
    assert g_within.reason == REASON_WITHIN_CEILING
    g_above = decide_business_action(state, 200_001)
    assert g_above.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL
    assert g_above.reason == REASON_ABOVE_CEILING


def test_D_BIZ_3b_autonomous_tier_null_ceiling_is_unbounded() -> None:
    """D-BIZ-3b — autonomous tier with a NULL ceiling = no ceiling = autonomous for any magnitude
    (the owner granted full autonomy; only a freeze escalates)."""
    state = _state(TIER_AUTONOMOUS, ceil=None)
    assert decide_business_action(state, 10**12).decision is BusinessActionDecision.AUTONOMOUS


def test_D_BIZ_4_frozen_wins_over_any_tier() -> None:
    """D-BIZ-4 — the kill switch: a frozen class requires owner approval regardless of tier or
    magnitude — even a tier=autonomous, NULL-ceiling, magnitude-0 action is gated when frozen."""
    for state in (
        _state(TIER_THRESHOLD, below=10**9, frozen=True),
        _state(TIER_AUTONOMOUS, ceil=None, frozen=True),
    ):
        gate = decide_business_action(state, 0)
        assert gate.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL
        assert gate.reason == REASON_FROZEN


def test_D_BIZ_5_negative_magnitude_and_unknown_tier_fail_closed() -> None:
    """D-BIZ-5 — a negative magnitude is never autonomous (a refund/credit is still consequential),
    and an unknown future tier value falls through to owner approval (forward-compat fail-closed)."""
    assert decide_business_action(_state(TIER_AUTONOMOUS, ceil=None), -1).reason == REASON_NEGATIVE_MAGNITUDE
    unknown = _state("some_future_tier", below=1)
    g = decide_business_action(unknown, 0)
    assert g.decision is BusinessActionDecision.REQUIRES_OWNER_APPROVAL
    assert g.reason == REASON_NO_AUTONOMY_SETTING


# ===========================================================================
# Layer B — THE TRANSPORT CHOKE (no DB): the effect boundary fails closed
# ===========================================================================


def test_D_BIZ_6_effect_outside_context_raises() -> None:
    """D-BIZ-6 — the structural choke: asserting the context (what every effect does first) OUTSIDE
    business_action_context raises. A direct caller that skipped the gate cannot run the effect."""
    from orchestrator.agents.business_impact_choke import (
        UngatedBusinessActionError,
        assert_in_business_action_context,
    )

    with pytest.raises(UngatedBusinessActionError):
        assert_in_business_action_context(BusinessImpactClass.SPEND)


def test_D_BIZ_6b_sample_effect_outside_context_raises() -> None:
    """D-BIZ-6b — the SAMPLE effect itself fails closed when called directly (bypassing propose_spend
    + the gate): _apply_spend_effect raises because it is not inside the gated context."""
    from orchestrator.agents.business_impact_choke import UngatedBusinessActionError
    from orchestrator.agents.business_impact_sample import _apply_spend_effect

    with pytest.raises(UngatedBusinessActionError):
        _apply_spend_effect(UUID(int=1), 100, label="bypass")


def test_D_BIZ_7_effect_inside_context_is_admitted() -> None:
    """D-BIZ-7 — a gate-approved/owner-approved path enters the context; the effect is then admitted
    (the choke lets a properly-gated action through; it does not block legitimate work)."""
    from orchestrator.agents.business_impact_choke import (
        assert_in_business_action_context,
        business_action_context,
    )

    with business_action_context(BusinessImpactClass.SPEND):
        assert_in_business_action_context(BusinessImpactClass.SPEND)  # no raise

    # context exits → the boundary is closed again
    from orchestrator.agents.business_impact_choke import UngatedBusinessActionError

    with pytest.raises(UngatedBusinessActionError):
        assert_in_business_action_context(BusinessImpactClass.SPEND)


# ===========================================================================
# Layer C — THE CAPABILITY GUARD (langchain): brain holds NO write tool
# ===========================================================================

_HAS_LANGCHAIN = True
try:  # pragma: no cover - import probe
    import langchain  # noqa: F401
except Exception:  # noqa: BLE001
    _HAS_LANGCHAIN = False

requires_langchain = pytest.mark.skipif(
    not _HAS_LANGCHAIN, reason="langchain not installed — capability-rail proof skipped"
)


@requires_langchain
@pytest.mark.parametrize(
    "evil_tool_name",
    [
        "execute_spend_now",
        "commit_spend_for_boost",
        "make_payment_to_vendor",
        "charge_card_on_file",
        "place_order_with_supplier",
        "create_purchase_order",
        "make_commitment_to_customer",
        "place_booking_for_owner",
        "sign_contract_with_vendor",
        "write_config_to_gbp",
        "update_integration_config_shopify",
        "push_config_change",
        "apply_config_change_now",
    ],
)
def test_D_BIZ_8_brain_cannot_hold_a_business_effect_tool(evil_tool_name: str) -> None:
    """D-BIZ-8 — handing the agent builder a spend/commit/config-WRITE tool RAISES at graph build
    (the VT-268 forbidden-set EXTENDED for VT-467). The brain can never structurally hold a tool that
    commits money / makes a commitment / changes config directly — proven against the real builder."""
    from langchain_core.tools import tool

    from orchestrator.agent.orchestrator_agent import _MODEL, build_orchestrator_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    def _impl(x: str) -> str:
        """A would-be business-effect tool that must never reach the agent surface."""
        return x

    evil = tool(evil_tool_name)(_impl)
    with pytest.raises(ToolGuardrailViolation):
        build_orchestrator_agent(_MODEL, extra_tools=[evil])


@requires_langchain
def test_D_BIZ_8b_benign_read_tools_not_false_flagged() -> None:
    """D-BIZ-8b — defense in depth: benign READ tools (query_spend, get_payment_status, read_config)
    are NOT flagged by the extended forbidden set (the substrings name WRITE/EXECUTE capability, not a
    bare 'spend'/'pay'/'config'). A too-broad guard would break legitimate read tools."""
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    benign = [
        SimpleNamespace(name="query_spend_total"),
        SimpleNamespace(name="get_payment_status"),
        SimpleNamespace(name="read_config"),
        SimpleNamespace(name="list_commitments"),
    ]
    assert find_forbidden_tools(benign) == []


# ===========================================================================
# Layer D — DB-BACKED end-to-end through the SAMPLE action (real RLS)
# ===========================================================================

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards

from orchestrator.agents import business_impact_choke as choke  # noqa: E402
from orchestrator.agents.business_impact_sample import propose_spend  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-467 DB-backed proof tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists (mirrors VT-460 proof)."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt467-bizproof-salt")
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
    """Seed a minimal tenant (the business-autonomy gate needs only the tenants FK)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, business_type, "
            "verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', 'gstin_verified', %s) "
            "RETURNING id",
            ("VT-467 bizproof", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _run_id(dsn: str, tenant: UUID) -> UUID:
    """A pipeline_runs row the approval can hang off (pending_approvals.run_id FKs it)."""
    rid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'business_impact', 'running')",
            (str(rid), str(tenant)),
        )
    return rid


@requires_db
def test_D_BIZ_9_no_grant_routes_to_owner_approval_effect_not_run(substrate):  # type: ignore[no-untyped-def]
    """D-BIZ-9 — THE fail-closed default through the real path: a tenant with NO business-autonomy
    row → propose_spend gates to owner approval (effect NOT run), AND get_business_autonomy returns
    the always_approve floor for the missing row. Owner approval armed via dry_run (no Twilio)."""
    tenant = _new_tenant(substrate.dsn)
    rid = _run_id(substrate.dsn, tenant)

    # The missing-row read is the fail-closed floor.
    with tenant_connection(tenant) as conn:
        state = choke.get_business_autonomy(tenant, BusinessImpactClass.SPEND, conn=conn)
    assert state.tier == TIER_ALWAYS_APPROVE and state.frozen is False

    with tenant_connection(tenant) as conn:
        outcome = propose_spend(tenant, rid, 1, conn=conn, dry_run=True)

    assert outcome.decision == BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value
    assert outcome.executed is False
    assert outcome.reason == REASON_ALWAYS_APPROVE_TIER
    assert outcome.approval_status == "armed"  # routed through the EXISTING arm_pause_request


@requires_db
def test_D_BIZ_10_granted_threshold_below_autonomous_at_or_above_approval(substrate):  # type: ignore[no-untyped-def]
    """D-BIZ-10 — a granted THRESHOLD tier: a spend BELOW the threshold runs autonomously (the effect
    executes, no approval armed); a spend AT/ABOVE the threshold routes to owner approval. The grant
    is the owner's deterministic loosening (the decay)."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        choke.grant_business_autonomy(
            tenant, BusinessImpactClass.SPEND, tier=TIER_THRESHOLD,
            auto_approve_below_minor=50_000, conn=conn,
        )

    # below → autonomous (effect runs)
    with tenant_connection(tenant) as conn:
        below = propose_spend(tenant, _run_id(substrate.dsn, tenant), 49_999, conn=conn, dry_run=True)
    assert below.decision == BusinessActionDecision.AUTONOMOUS.value
    assert below.executed is True and below.reason == REASON_BELOW_THRESHOLD

    # at/above → owner approval (effect NOT run)
    with tenant_connection(tenant) as conn:
        above = propose_spend(tenant, _run_id(substrate.dsn, tenant), 50_000, conn=conn, dry_run=True)
    assert above.decision == BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value
    assert above.executed is False and above.reason == REASON_AT_OR_ABOVE_THRESHOLD
    assert above.approval_status == "armed"


@requires_db
def test_D_BIZ_11_granted_autonomous_ceiling_within_then_escalates(substrate):  # type: ignore[no-untyped-def]
    """D-BIZ-11 — a granted AUTONOMOUS tier with a ceiling: within the ceiling runs autonomously,
    above it escalates to owner approval (the extreme-scenario line)."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        choke.grant_business_autonomy(
            tenant, BusinessImpactClass.SPEND, tier=TIER_AUTONOMOUS,
            autonomous_ceiling_minor=200_000, conn=conn,
        )

    with tenant_connection(tenant) as conn:
        within = propose_spend(tenant, _run_id(substrate.dsn, tenant), 200_000, conn=conn, dry_run=True)
    assert within.decision == BusinessActionDecision.AUTONOMOUS.value and within.executed is True

    with tenant_connection(tenant) as conn:
        above = propose_spend(tenant, _run_id(substrate.dsn, tenant), 200_001, conn=conn, dry_run=True)
    assert above.decision == BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value
    assert above.executed is False and above.reason == REASON_ABOVE_CEILING


@requires_db
def test_D_BIZ_12_frozen_class_approval_even_below_threshold(substrate):  # type: ignore[no-untyped-def]
    """D-BIZ-12 — the kill switch through the real path: a class with a generous granted threshold,
    then FROZEN, routes EVERY spend to owner approval — even one far below the old threshold. A freeze
    tightens autonomy back to the floor (the regression/decay-tighten path)."""
    tenant = _new_tenant(substrate.dsn)
    with tenant_connection(tenant) as conn:
        choke.grant_business_autonomy(
            tenant, BusinessImpactClass.SPEND, tier=TIER_THRESHOLD,
            auto_approve_below_minor=10**9, conn=conn,
        )
        # sanity: before the freeze, a tiny spend is autonomous
    with tenant_connection(tenant) as conn:
        pre = propose_spend(tenant, _run_id(substrate.dsn, tenant), 100, conn=conn, dry_run=True)
    assert pre.decision == BusinessActionDecision.AUTONOMOUS.value

    with tenant_connection(tenant) as conn:
        choke.freeze_business_class(tenant, BusinessImpactClass.SPEND, True, reason="owner_kill", conn=conn)
    with tenant_connection(tenant) as conn:
        post = propose_spend(tenant, _run_id(substrate.dsn, tenant), 100, conn=conn, dry_run=True)
    assert post.decision == BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value
    assert post.executed is False and post.reason == REASON_FROZEN
