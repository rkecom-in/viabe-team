"""VT-267 PR-A — prereq substrate: tenants.owner_inputs/created_via (mig 065) +
business_profile MERGE-not-clobber. Real Postgres, no mock cursors (DR-15)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-267 PR-A DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-267 PR-A test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


# --- mig 065: owner_inputs consent gate + created_via -------------------------

@_DB
def test_owner_inputs_column_exists_and_gate_works(db_ctx):
    from uuid import UUID

    from orchestrator.memory.l0_writer import _owner_inputs_enabled

    t = _tenant(db_ctx.dsn)
    assert _owner_inputs_enabled(UUID(t)) is False  # fresh tenant → fail-closed default
    with psycopg.connect(db_ctx.dsn, autocommit=True) as conn:
        conn.execute("UPDATE tenants SET owner_inputs = TRUE WHERE id = %s", (t,))
    assert _owner_inputs_enabled(UUID(t)) is True


@_DB
def test_created_via_column_exists(db_ctx):
    t = _tenant(db_ctx.dsn)
    with psycopg.connect(db_ctx.dsn, autocommit=True) as conn:
        conn.execute("UPDATE tenants SET created_via = %s WHERE id = %s", ("whatsapp", t))
        v = conn.execute("SELECT created_via FROM tenants WHERE id = %s", (t,)).fetchone()[0]
    assert v == "whatsapp"


# --- business_profile MERGE-not-clobber ---------------------------------------

def _profile(tenant: str):
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    return (row["attributes"] if isinstance(row, dict) else row[0]) if row else None


@_DB
def test_merge_not_clobber(db_ctx):
    from orchestrator.knowledge.l1 import upsert_business_profile

    t = _tenant(db_ctx.dsn)
    upsert_business_profile(t, {"archetype": "kirana", "owner_persona": "value"})
    upsert_business_profile(t, {"gbp_context": {"rating": 4.6}})  # sibling write
    attrs = _profile(t)
    assert attrs["archetype"] == "kirana"          # preserved
    assert attrs["owner_persona"] == "value"        # preserved
    assert attrs["gbp_context"]["rating"] == 4.6    # merged in

    # A key present in the new write overwrites; siblings still preserved.
    upsert_business_profile(t, {"archetype": "restaurant"})
    attrs = _profile(t)
    assert attrs["archetype"] == "restaurant"       # overwritten
    assert attrs["gbp_context"]["rating"] == 4.6    # still preserved


@_DB
def test_business_profile_cross_tenant_isolation(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.l1 import upsert_business_profile

    a, b = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    upsert_business_profile(a, {"archetype": "kirana"})
    with tenant_connection(b) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM l1_entities WHERE entity_type='business_profile'"
        ).fetchone()["n"]
    assert n == 0  # B cannot see A's business_profile (RLS)
