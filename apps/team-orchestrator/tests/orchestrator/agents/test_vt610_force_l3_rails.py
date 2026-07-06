"""VT-610 — the MUST-NOT-BYPASS rail proof suite (loop program Package 7).

``autonomy.force_l3`` bypasses ONLY two things: the earning threshold (``clean_approval_streak``)
and owner opt-in (no ``approval_id`` — a verified VTR id + scrubbed reason substitute). Recon's
binding rail list — each gets its own proof here that a forced-L3 (tenant, agent) is
INDISTINGUISHABLE, to every OTHER gate in the system, from an un-forced one:

  1. Gate-0 activation      (onboarding_gate.is_agent_eligible) — reads onboarding_journey /
     tenants.verification_status / tenant_connector_status / customers; NEVER tenant_agent_autonomy.
  2. Per-recipient consent/opt-out/complaint/caps (customer_send.agent_send_draft's gate stack) —
     the SAME code path runs for a forced-L3 send as an earned one; ``autonomy_level="L3"`` is a
     caller-supplied ROUTING parameter, never a provenance-aware bypass. Two proofs: an opted-out
     customer trips Gate-3 (SKIP_OPT_OUT); a subscribed-but-unconsented one reaches past Gate-3 and
     is refused at Gate-4, the marketing-consent C2 allowlist (SKIP_CONSENT) — the on-point proof
     that a forced-L3 agent still cannot send to a customer who never consented.
  3. Policy boundary        (business_policy.assert_within_policy) — a SEPARATE table
     (tenant_business_policy) force_l3 never writes.
  4. Always-confirm floor   (autonomy.is_always_confirm) — re-derived PER BATCH; the function has
     no ``level`` parameter at all, forced or earned makes zero difference to it.
  5. Business-impact gates  (business_impact_choke.assert_or_gate_business_action +
     tenant_business_autonomy) — a THIRD separate table force_l3 never touches.
  6. RLS / tenant scoping   — force_l3 is tenant_id-parameterized (proven at the primitive level in
     test_autonomy.py); the endpoint-level operator-assignment gate is proven in
     test_ops_vtr_console.py. Not re-proven here.
  7. Regression freeze      — a regression FREEZES/REVOKES a forced-L3 agent through the EXACT SAME
     ``record_regression_event`` path as an earned one: force grants level, never immunity.

DB substrate mirrors ``tests/orchestrator/agents/test_autonomy.py`` / ``test_customer_send.py``:
importorskip psycopg+dbos+langgraph, skipif no DATABASE_URL, module fixture apply_migrations +
launch_dbos; seeds via direct autocommit psycopg (service role, RLS bypassed at seed).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # tenant_connection -> orchestrator.graph pulls langgraph

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.agents import business_impact_choke as choke  # noqa: E402
from orchestrator.agents import business_policy as bp  # noqa: E402
from orchestrator.agents.autonomy import (  # noqa: E402
    force_l3,
    is_always_confirm,
    record_regression_event,
)
from orchestrator.agents.customer_send import (  # noqa: E402
    SKIP_CONSENT,
    SKIP_OPT_OUT,
    agent_send_draft,
)
from orchestrator.agents.onboarding_gate import is_agent_eligible  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-610 force_l3 rail tests skipped",
)

pytestmark = requires_db

AGENT = "sales_recovery"
_TEMPLATE = "team_winback_simple"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) --------------------


def _new_tenant(dsn: str, *, onboarded: bool = False) -> UUID:
    """A bare (NOT onboarded by default) tenant — the honest baseline force_l3 is tested against:
    force_l3 must grant NOTHING toward any of these OTHER rails."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('VT-610 rail test', 'founding', 'trial', now(), 'restaurant', %s) "
            "RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()
        assert row is not None
        tenant = UUID(str(row[0]))
        if onboarded:
            conn.execute(
                "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, "
                "last_status, last_ingested_date) VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE)",
                (str(tenant), f"conn-{uuid4().hex[:8]}"),
            )
            conn.execute(
                "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
                "VALUES (%s, 'complete', now())",
                (str(tenant),),
            )
            conn.execute(
                "UPDATE tenants SET verification_status = 'gstin_verified', "
                "ownership_verified = TRUE WHERE id = %s",
                (str(tenant),),
            )
            conn.execute(
                "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
                "VALUES (%s, 'live', %s)",
                (str(tenant), f"+9180{uuid4().int % 10**8:08d}"),
            )
    return tenant


def _seed_customer(dsn: str, tenant: UUID, *, opt_out_status: str = "subscribed") -> tuple[UUID, str]:
    phone = f"+9197{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status) "
            "VALUES (%s, 'Rail Cust', %s, %s) RETURNING id",
            (str(tenant), phone, opt_out_status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0])), phone


def _seed_work_item(dsn: str, tenant: UUID, *, status: str = "approved") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", AGENT, status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, work_item: UUID, *, status: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(work_item), AGENT, status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(dsn: str, tenant: UUID, batch: UUID, customer: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(batch), str(customer), _TEMPLATE),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_autonomy_row(dsn: str, tenant: UUID, *, streak: int = 0) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_agent_autonomy (tenant_id, agent, clean_approval_streak) "
            "VALUES (%s, %s, %s)",
            (str(tenant), AGENT, streak),
        )


def _force(tenant: UUID) -> None:
    """Force L3 for AGENT on tenant — the precondition every rail test below applies against."""
    with tenant_connection(tenant) as conn:
        st = force_l3(tenant, AGENT, vtr_id=str(uuid4()), reason="rail proof", conn=conn)
    assert st.level == "L3"  # sanity: the force actually landed before we test what it did NOT do


# ---------------------------------------------------------------------------
# 1. Gate-0 activation (onboarding_gate.is_agent_eligible) — untouched by force_l3
# ---------------------------------------------------------------------------


def test_force_l3_does_not_bypass_gate0_activation(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)  # deliberately NOT onboarded
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    with tenant_connection(tenant) as conn:
        assert is_agent_eligible(tenant, AGENT, conn=conn) is False


# ---------------------------------------------------------------------------
# 2. Per-recipient consent/opt-out/complaint/caps (customer_send.agent_send_draft)
# ---------------------------------------------------------------------------


def test_force_l3_does_not_bypass_customer_send_opt_out_gate(substrate) -> None:
    """A FULLY onboarded tenant (so the send reaches the per-recipient gates, not Gate-0/0b),
    an L3-shaped batch (auto_send_pending — the only batch state agent_send_draft accepts for the
    L3 arm), and an OPTED-OUT customer: even with L3 forced, the send is skipped at the opt-out
    gate — force_l3 granted a LEVEL, not a bypass of the send stack itself."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn, onboarded=True)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    customer, _phone = _seed_customer(dsn, tenant, opt_out_status="opted_out")
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item, status="auto_send_pending")
    draft = _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        result = agent_send_draft(tenant, draft, autonomy_level="L3", conn=conn)

    assert result.status == "skipped"
    assert result.skip_reason == SKIP_OPT_OUT


def test_force_l3_does_not_bypass_marketing_consent_gate(substrate) -> None:
    """The on-point proof: an opted-out customer trips Gate-3 BEFORE the send stack ever reaches
    Gate-4, so the OPT-OUT test above never exercises the marketing-consent gate at all. Here the
    customer is SUBSCRIBED (Gate-3 passes) but carries NO ``record_of_consent`` row — the real
    production hard-stop C2 allowlist (customer_send.py Gate-4, ``has_marketing_consent_for_phone``,
    fail-closed on an empty/no-match allowlist) must still refuse the send under a FORCED L3, exactly
    as it would for an earned one. This is the row's whole safety claim: a forced-L3 agent still
    cannot send to a customer who never consented."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn, onboarded=True)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    customer, _phone = _seed_customer(dsn, tenant)  # default: subscribed, no consent row seeded
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item, status="auto_send_pending")
    draft = _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        result = agent_send_draft(tenant, draft, autonomy_level="L3", conn=conn)

    assert result.status == "skipped"
    assert result.skip_reason == SKIP_CONSENT


# ---------------------------------------------------------------------------
# 3. Policy boundary (business_policy.assert_within_policy) — a separate table
# ---------------------------------------------------------------------------


def test_force_l3_does_not_bypass_policy_boundary(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    with tenant_connection(tenant) as conn:
        # No tenant_business_policy row exists — force_l3 never wrote one (different table).
        check = bp.assert_within_policy(
            tenant, bp.PolicyActionClass.CUSTOMER_SEND, {"segment": "lapsed"}, conn=conn
        )
    assert check.out_of_policy
    assert check.reason == bp.REASON_ACTION_TYPE_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 4. Always-confirm floor (autonomy.is_always_confirm) — level-blind by construction
# ---------------------------------------------------------------------------


def test_force_l3_does_not_bypass_always_confirm_floor(substrate) -> None:
    """is_always_confirm has no ``level``/``autonomy_state`` parameter at all — it is re-derived
    PER BATCH from money/bulk/first-contact/novel-template signals only. Forcing L3 changes
    NOTHING about what this predicate returns for the SAME batch shape."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    fresh_customer, _phone = _seed_customer(dsn, tenant)  # no agent_customer_contacts row

    with tenant_connection(tenant) as conn:
        floor, reason = is_always_confirm(
            tenant, agent=AGENT, batch_customer_ids=[str(fresh_customer)],
            template_name=_TEMPLATE, money_bearing=False, conn=conn,
        )
    assert floor is True
    assert reason == "first_contact"


# ---------------------------------------------------------------------------
# 5. Business-impact gates (business_impact_choke + tenant_business_autonomy) — a separate table
# ---------------------------------------------------------------------------


def test_force_l3_grants_nothing_toward_business_impact_gates(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    with tenant_connection(tenant) as conn:
        # No tenant_business_autonomy row exists — force_l3 (tenant_agent_autonomy) never
        # touches this SEPARATE table. Fail-closed default: always_approve.
        gate = choke.assert_or_gate_business_action(
            tenant, choke.BusinessImpactClass.SPEND, 100_00, conn=conn
        )
    assert gate.requires_owner_approval


# ---------------------------------------------------------------------------
# 7. Regression freeze/revoke — a forced-L3 agent regresses IDENTICALLY to an earned one
# ---------------------------------------------------------------------------


def test_regression_revokes_forced_l3_identically_to_earned(substrate) -> None:
    """A non-freezing regression kind ('edit') REVOKES at L3 regardless of HOW L3 was reached —
    force grants level, never immunity to a regression."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)

    with tenant_connection(tenant) as conn:
        st = record_regression_event(tenant, AGENT, "edit", conn=conn)

    assert st.level == "L2"  # revoked — one-way per incident, same as an earned L3's regression
    assert st.clean_approval_streak == 0
    assert st.frozen is False  # 'edit' revokes but does not freeze


def test_freezing_regression_freezes_forced_l3_identically_to_earned(substrate) -> None:
    """A kill-switch regression kind ('optout_spike') FREEZES a forced-L3 agent AND revokes it to
    L2 (§5.4: at L3, any regression except owner_disengaged revokes — freeze and revoke both fire
    together here, exactly as they would for an EARNED L3) + cancels its open batches atomically.
    No immunity to freeze, no immunity to revoke — force grants level, nothing else."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant)
    _force(tenant)
    customer, _phone = _seed_customer(dsn, tenant)
    work_item = _seed_work_item(dsn, tenant, status="drafting")
    batch = _seed_batch(dsn, tenant, work_item, status="auto_send_pending")  # the L3 hold window
    _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        st = record_regression_event(tenant, AGENT, "optout_spike", conn=conn)

    assert st.frozen is True
    assert st.level == "L2"  # revoked too — at L3, any non-owner_disengaged regression revokes
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    assert row[0] == "cancelled"  # the binding atomic-cancel rule, forced-L3 included
