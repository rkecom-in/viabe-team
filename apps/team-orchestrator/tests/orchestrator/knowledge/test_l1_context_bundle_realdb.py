"""VT-195 Phase 1 — real-DB tests for the L1 Context Composer read path.

assemble_context_bundle reads the tenant's 'business_profile' l1_entities entity
(RLS-scoped via search_entities -> tenant_connection -> app_role) and renders the
pre-inject block. Also verifies the get_business_profile reconcile (orphaned
tenant_l1_profile probe -> l1_entities) and a REAL cross-tenant RLS denial
(VT-263 lesson: seed a B-owned entity, assert it is invisible under A's GUC — not
a WHERE-clause-shaped tautology).

Requires DATABASE_URL + the dbos stack; runs in the CI orchestrator job. CL-422
synthetic data only. CL-390: no customer PII (owner_curated_context is
owner-authored business context).
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
    reason="DATABASE_URL not set — VT-195 L1 context-bundle tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (incl. 019 L1 KG) + launch DBOS so the pool exists."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str, name: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES (%s, 'founding', 'onboarding') RETURNING id",
            (name,),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_business_profile(dsn: str, tenant_id: UUID, attributes: dict) -> None:
    """Seed a 'business_profile' entity via superuser (RLS bypassed at seed time;
    the production read path is what we test under RLS)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO l1_entities (tenant_id, entity_type, attributes) "
            "VALUES (%s, 'business_profile', %s::jsonb)",
            (str(tenant_id), json.dumps(attributes)),
        )


def test_assemble_context_bundle_renders_tenant_identity(substrate):
    from orchestrator.knowledge.l1 import assemble_context_bundle

    a = _new_tenant(substrate.dsn, "Alpha Electronics")
    _seed_business_profile(
        substrate.dsn,
        a,
        {
            "business_archetype": "electronics_retail",
            "owner_persona": "direct, cost-conscious, prefers Hindi",
            "working_hours": "10:00-20:00 IST",
            "owner_curated_context": "Festival season is peak; values repeat buyers.",
        },
    )

    block = assemble_context_bundle(a)
    assert block is not None
    assert "Tenant context (L1)" in block
    assert "electronics_retail" in block
    assert "Festival season is peak" in block


def test_assemble_context_bundle_none_when_no_entity(substrate):
    from orchestrator.knowledge.l1 import assemble_context_bundle

    a = _new_tenant(substrate.dsn, "No-Profile Co")
    assert assemble_context_bundle(a) is None  # nothing to inject


def test_assemble_context_bundle_cross_tenant_rls_denial(substrate):
    """REAL RLS check: B's business_profile is invisible under A's GUC."""
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.l1 import assemble_context_bundle

    a = _new_tenant(substrate.dsn, "Tenant A RLS")
    b = _new_tenant(substrate.dsn, "Tenant B RLS")
    _seed_business_profile(substrate.dsn, a, {"business_archetype": "a_archetype"})
    _seed_business_profile(
        substrate.dsn, b, {"business_archetype": "b_archetype_SECRET"}
    )

    # A's bundle contains A's data, never B's.
    block_a = assemble_context_bundle(a)
    assert block_a is not None and "a_archetype" in block_a
    assert "b_archetype_SECRET" not in block_a

    # Real RLS backstop: under A's GUC (app_role, RLS enforced), B's entity is
    # invisible — B's row genuinely exists, so 0 means RLS hid it (not a WHERE
    # filter). This is the VT-263 lesson applied.
    with tenant_connection(a) as conn:
        leaked = conn.execute(
            "SELECT count(*) AS n FROM l1_entities WHERE tenant_id = %s",
            (str(b),),
        ).fetchone()
    n = leaked["n"] if isinstance(leaked, dict) else leaked[0]
    assert n == 0


def test_upsert_business_profile_idempotent(substrate):
    """VT-195 Phase 3: upsert_business_profile is idempotent (one row per tenant,
    re-run updates in place) and the rendered bundle reflects the latest values."""
    import psycopg

    from orchestrator.knowledge import assemble_context_bundle, upsert_business_profile

    a = _new_tenant(substrate.dsn, "Upsert Co")
    id1 = upsert_business_profile(
        a, {"business_archetype": "archetype_v1", "owner_curated_context": "note v1"}
    )
    id2 = upsert_business_profile(
        a, {"business_archetype": "archetype_v2", "owner_curated_context": "note v2"}
    )
    assert id1 == id2  # same entity — upsert, not a duplicate

    block = assemble_context_bundle(a)
    assert block is not None
    assert "archetype_v2" in block and "archetype_v1" not in block  # updated in place

    # Exactly one business_profile row for the tenant (the partial unique index).
    with psycopg.connect(substrate.dsn) as conn:
        n = conn.execute(
            "SELECT count(*) FROM l1_entities WHERE tenant_id = %s "
            "AND entity_type = 'business_profile'",
            (str(a),),
        ).fetchone()[0]
    assert n == 1


def test_get_business_profile_reads_l1_owner_curated_context(substrate):
    """VT-195 reconcile: get_business_profile reads owner_curated_context from the
    l1_entities 'business_profile' entity (was an orphaned tenant_l1_profile probe)."""
    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        get_business_profile,
    )
    from orchestrator.graph import get_pool

    a = _new_tenant(substrate.dsn, "Reconcile Co")
    _seed_business_profile(
        substrate.dsn,
        a,
        {"owner_curated_context": "L1-sourced owner note (VT-195)."},
    )

    out = get_business_profile(GetBusinessProfileInput(tenant_id=str(a)), pool=get_pool())
    assert out is not None
    assert out.owner_curated_context == "L1-sourced owner note (VT-195)."
