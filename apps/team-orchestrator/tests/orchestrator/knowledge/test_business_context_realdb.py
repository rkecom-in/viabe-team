"""VT-466 — real-DB tests for the Team-Manager's business-context READ + WRITE + slice seams.

The manager READS a coherent business context (L1 block + structured profile +
tenant identity + the manager-held objective), WRITES the per-tenant objective
back (MERGE-not-clobber, RLS-scoped), and produces LANE-SCOPED slices for
specialists. All of it composes over the EXISTING L1 ``business_profile`` entity
— no new store. These tests assert:

  - read assembles the business context INCL the objective;
  - write records + reads back the objective TENANT-scoped, MERGE-not-clobber;
  - a specialist slice is SCOPED (no cross-tenant; lane-scoped — an unmapped lane
    gets identity-only, NOT the full profile);
  - a REAL cross-tenant RLS denial on the objective write (VT-263 lesson: seed a
    B-owned objective, assert it is invisible under A's read).

Requires DATABASE_URL + the dbos stack; runs in the CI orchestrator job. CL-422
synthetic data only. CL-390: business context only — no customer PII.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("dbos")
pytest.importorskip("pgvector")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-466 business-context tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (incl. 019 L1 KG) + launch DBOS so the pool exists."""
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


def _new_tenant(
    dsn: str,
    name: str,
    *,
    business_type: str = "cafe",
    verification_status: str = "gstin_verified",
    verified_business_name: str | None = "Verified Co Pvt Ltd",
    gstin: str | None = "27ABCDE1234F1Z5",
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, business_type, "
            "verification_status, verified_business_name, gstin) "
            "VALUES (%s, 'founding', 'onboarding', %s, %s, %s, %s) RETURNING id",
            (name, business_type, verification_status, verified_business_name, gstin),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_business_profile(dsn: str, tenant_id: UUID, attributes: dict) -> None:
    """Seed a 'business_profile' entity via superuser (RLS bypassed at seed time;
    the production read/write path is what we test under RLS)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, attributes) "
            "VALUES (%s, 'business_profile', %s::jsonb)",
            (str(tenant_id), json.dumps(attributes)),
        )


# --- READ seam -------------------------------------------------------------


def test_read_business_context_assembles_identity_profile_and_objective(substrate):
    """The manager read assembles the business context including the objective."""
    from orchestrator.knowledge import read_business_context, write_business_objective

    a = _new_tenant(substrate.dsn, "Read Cafe")
    _seed_business_profile(
        substrate.dsn,
        a,
        {
            "business_archetype": "cafe_qsr",
            "working_hours": "08:00-22:00",
            "communication_prefs": "Hindi, concise",
            "owner_curated_context": "Festival season is peak.",
        },
    )
    # Record an objective via the WRITE seam, then read it back via READ.
    write_business_objective(
        a, {"objective": "grow repeat orders 20%", "will": "festive push"}
    )

    ctx = read_business_context(a)

    # Identity — verified name + verification status (the manager reasons off the
    # verified identity, not the owner-entered name).
    assert ctx.identity["verified_business_name"] == "Verified Co Pvt Ltd"
    assert ctx.identity["business_name"] == "Verified Co Pvt Ltd"
    assert ctx.identity["gst_verified"] is True
    assert ctx.identity["business_type"] == "cafe"

    # Structured profile (objective NOT double-rendered into the profile view).
    assert ctx.profile["business_archetype"] == "cafe_qsr"
    assert "business_objective" not in ctx.profile

    # Objective — the manager-held cross-turn context.
    assert ctx.objective["objective"] == "grow repeat orders 20%"
    assert ctx.objective["will"] == "festive push"

    # The L1 block is assembled (owner-stated profile renders).
    assert ctx.l1_block is not None and "cafe_qsr" in ctx.l1_block


def test_read_business_context_empty_tenant(substrate):
    """A tenant with no profile entity + no objective reads safe-empty, not error."""
    from orchestrator.knowledge import read_business_context

    a = _new_tenant(
        substrate.dsn,
        "Bare Co",
        verification_status="unverified",
        verified_business_name=None,
        gstin=None,
    )
    ctx = read_business_context(a)
    assert ctx.profile == {}
    assert ctx.objective == {}
    assert ctx.l1_block is None
    # Identity still resolves from the tenant row (unverified — name NOT verified).
    assert ctx.identity["gst_verified"] is False
    assert ctx.identity["verified_business_name"] is None


def test_render_business_context_block_carries_identity_and_objective(substrate):
    from orchestrator.knowledge import (
        read_business_context,
        render_business_context_block,
        write_business_objective,
    )

    a = _new_tenant(substrate.dsn, "Block Co")
    write_business_objective(a, {"objective": "raise AOV", "policy": "no discounts"})
    block = render_business_context_block(read_business_context(a))
    assert block is not None
    assert "## Business context" in block
    assert "Business objective" in block
    assert "raise AOV" in block
    assert "no discounts" in block
    assert "verified=True" in block  # gstin_verified default


# --- WRITE seam ------------------------------------------------------------


def test_write_business_objective_records_and_reads_back(substrate):
    """The write records + reads back tenant-scoped."""
    from orchestrator.knowledge import read_business_context, write_business_objective

    a = _new_tenant(substrate.dsn, "Write Co")
    write_business_objective(a, {"objective": "open a second outlet"})
    assert read_business_context(a).objective == {"objective": "open a second outlet"}


def test_write_business_objective_merges_not_clobbers(substrate):
    """A single learning never wipes the standing objective (MERGE-not-clobber)."""
    from orchestrator.knowledge import read_business_context, write_business_objective

    a = _new_tenant(substrate.dsn, "Merge Co")
    write_business_objective(
        a, {"objective": "grow repeat orders", "will": "festive push"}
    )
    merged = write_business_objective(a, {"learnings": "Tuesdays are slow"})
    # Patch added; siblings preserved.
    assert merged["objective"] == "grow repeat orders"
    assert merged["will"] == "festive push"
    assert merged["learnings"] == "Tuesdays are slow"
    assert read_business_context(a).objective == merged


def test_write_business_objective_preserves_sibling_profile_attrs(substrate):
    """Writing the objective must NOT clobber other business_profile attributes
    (the objective is ONE key on the shared entity)."""
    from orchestrator.knowledge import read_business_context, write_business_objective

    a = _new_tenant(substrate.dsn, "Sibling Co")
    _seed_business_profile(substrate.dsn, a, {"business_archetype": "salon"})
    write_business_objective(a, {"objective": "fill weekday slots"})
    ctx = read_business_context(a)
    assert ctx.profile["business_archetype"] == "salon"  # sibling preserved
    assert ctx.objective == {"objective": "fill weekday slots"}


def test_write_empty_patch_is_noop(substrate):
    from orchestrator.knowledge import read_business_context, write_business_objective

    a = _new_tenant(substrate.dsn, "Noop Co")
    write_business_objective(a, {"objective": "x"})
    assert write_business_objective(a, {}) == {"objective": "x"}
    assert read_business_context(a).objective == {"objective": "x"}


# --- SLICE seam ------------------------------------------------------------


def test_context_slice_is_lane_scoped(substrate):
    """A specialist slice is scoped: lane-relevant profile keys + objective ONLY."""
    from orchestrator.knowledge import (
        context_slice_for_lane,
        read_business_context,
        write_business_objective,
    )

    a = _new_tenant(substrate.dsn, "Slice Co")
    _seed_business_profile(
        substrate.dsn,
        a,
        {
            "business_archetype": "cafe_qsr",
            "working_hours": "08:00-22:00",
            "communication_prefs": "Hindi",
            "integration_map": {"shopify": "connected"},
            "owner_persona": "hands-on",
        },
    )
    write_business_objective(a, {"objective": "grow repeat orders"})
    ctx = read_business_context(a)

    sales = context_slice_for_lane(ctx, "sales_recovery")
    assert sales["lane"] == "sales_recovery"
    # Sales lane sees its keys; NOT integration_map / owner_persona.
    assert set(sales["profile"]) == {
        "business_archetype",
        "working_hours",
        "communication_prefs",
    }
    assert "integration_map" not in sales["profile"]
    assert sales["objective"] == {"objective": "grow repeat orders"}

    # Integration lane sees a DIFFERENT slice (its keys only).
    integ = context_slice_for_lane(ctx, "integration")
    assert set(integ["profile"]) == {"business_archetype", "integration_map"}
    assert "working_hours" not in integ["profile"]


def test_context_slice_unmapped_lane_is_identity_only(substrate):
    """An unmapped lane (e.g. a future 'finance') gets the identity anchor ONLY —
    default-deny, so a new lane is never accidentally handed the full profile."""
    from orchestrator.knowledge import context_slice_for_lane, read_business_context

    a = _new_tenant(substrate.dsn, "Unmapped Co")
    _seed_business_profile(
        substrate.dsn,
        a,
        {
            "business_archetype": "cafe_qsr",
            "working_hours": "08:00-22:00",
            "integration_map": {"shopify": "connected"},
        },
    )
    ctx = read_business_context(a)
    fin = context_slice_for_lane(ctx, "finance")
    assert fin["lane"] == "finance"
    assert set(fin["profile"]) == {"business_archetype"}  # identity anchor only
    assert "working_hours" not in fin["profile"]
    assert "integration_map" not in fin["profile"]


# --- RLS / cross-tenant isolation ------------------------------------------


def test_objective_write_read_is_cross_tenant_isolated(substrate):
    """REAL RLS check: B's objective is invisible to A's read (not a WHERE-clause
    tautology — B's row genuinely exists; A's read returns it as empty)."""
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge import read_business_context, write_business_objective

    a = _new_tenant(substrate.dsn, "Tenant A obj-RLS")
    b = _new_tenant(substrate.dsn, "Tenant B obj-RLS")
    write_business_objective(a, {"objective": "A_objective_PUBLIC"})
    write_business_objective(b, {"objective": "B_objective_SECRET"})

    # A reads ONLY A's objective.
    ctx_a = read_business_context(a)
    assert ctx_a.objective == {"objective": "A_objective_PUBLIC"}

    # Real RLS backstop: under A's GUC, B's business_profile entity is invisible
    # (B's row exists, so 0 means RLS hid it — VT-263 lesson).
    with tenant_connection(a) as conn:
        leaked = conn.execute(
            "SELECT count(*) AS n FROM l1_entities "
            "WHERE tenant_id = %s AND entity_type = 'business_profile'",
            (str(b),),
        ).fetchone()
    n = leaked["n"] if isinstance(leaked, dict) else leaked[0]
    assert n == 0


# --- handoff slice wiring (VT-465 SpecialistHandoff.context_slice) ---------


def test_handoff_update_populates_context_slice(substrate):
    """build_handoff_update wires the lane-scoped slice into the standard
    SpecialistHandoff envelope's context_slice (VT-465 ← VT-466).

    Uses the ``integration`` spec — its per-lane update_builder is minimal
    (returns {} after a fail-loud tenant/run check), so the test exercises the
    SLICE wiring without standing up a full SalesRecoveryContext bundle."""
    from uuid import uuid4

    from orchestrator.agent.roster import (
        HANDOFF_STATE_KEY,
        build_handoff_update,
        get_spec,
    )
    from orchestrator.knowledge import write_business_objective

    a = _new_tenant(substrate.dsn, "Handoff Co")
    _seed_business_profile(
        substrate.dsn,
        a,
        {"business_archetype": "cafe_qsr", "integration_map": {"shopify": "connected"}},
    )
    write_business_objective(a, {"objective": "connect the store"})

    spec = get_spec("integration_agent")
    state = {"tenant_id": a, "run_id": uuid4(), "messages": []}
    update = build_handoff_update(spec=spec, state=state)
    envelope = update[HANDOFF_STATE_KEY]
    assert envelope.context_slice["lane"] == "integration"
    assert envelope.context_slice["objective"] == {"objective": "connect the store"}
    # Integration lane sees its keys only.
    assert "integration_map" in envelope.context_slice["profile"]


def test_handoff_update_no_tenant_yields_empty_slice(substrate):
    """A handoff with no tenant_id in state yields an empty slice, never an error
    (the per-lane data bundle the specialist consumed pre-VT-466 is unchanged).

    Note: the sales_recovery/integration update_builders fail-LOUD on a missing
    tenant_id (CL-195), so the no-tenant SLICE path is asserted directly on the
    slice helper — the production handoff always carries a tenant_id."""
    from orchestrator.agent.roster import _build_context_slice, get_spec

    spec = get_spec("sales_recovery_agent")
    assert _build_context_slice(spec=spec, state={"messages": []}) == {}
