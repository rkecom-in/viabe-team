"""Integration tests for the migration runner and RLS tenant isolation.

These require a live Postgres reachable via ``DATABASE_URL`` and the ``psycopg``
driver. They are skipped in the plain unit-test job and run in the dedicated
CI ``migrations`` job (which provisions a pgvector Postgres service).
"""

import os
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — migration integration tests skipped",
)

import apply_migrations  # noqa: E402 — imported after the psycopg skip guard


@pytest.fixture(scope="module")
def migrated():
    """Apply all migrations once against the fresh CI database."""
    dsn = os.environ["DATABASE_URL"]
    result = apply_migrations.apply(dsn=dsn)
    return {"dsn": dsn, "result": result}


def test_clean_apply(migrated):
    """Every migration applies cleanly on a fresh database."""
    result = migrated["result"]
    expected = [p.name for p in apply_migrations.migration_files()]

    assert result["failed"] == []
    assert result["applied"] == expected
    assert result["skipped"] == []

    # The base tables exist.
    with psycopg.connect(migrated["dsn"], autocommit=True) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    for table in ("tenants", "pipeline_runs", "phone_token_resolutions", "env_config"):
        assert table in tables


def test_rerun_is_noop(migrated):
    """Re-running the runner applies nothing — it is idempotent."""
    result = apply_migrations.apply(dsn=migrated["dsn"])
    expected = [p.name for p in apply_migrations.migration_files()]

    assert result["failed"] == []
    assert result["applied"] == []
    assert result["skipped"] == expected


def test_rls_blocks_cross_tenant(migrated):
    """RLS makes one tenant's rows invisible and unwritable to another."""
    dsn = migrated["dsn"]

    # Seed two tenants + a phase_transition each. The superuser bypasses RLS.
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Tenant A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Tenant B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO phase_transitions (tenant_id, to_phase) VALUES (%s, 'trial')",
            (tenant_a,),
        )
        conn.execute(
            "INSERT INTO phase_transitions (tenant_id, to_phase) VALUES (%s, 'trial')",
            (tenant_b,),
        )
        # A non-superuser role so RLS is actually enforced (superusers bypass).
        conn.execute("DROP ROLE IF EXISTS rls_tester")
        conn.execute("CREATE ROLE rls_tester NOLOGIN")
        conn.execute("GRANT USAGE ON SCHEMA public TO rls_tester")
        conn.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON ALL TABLES IN SCHEMA public TO rls_tester"
        )
        conn.execute("GRANT EXECUTE ON FUNCTION app_current_tenant() TO rls_tester")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # Scoped to tenant A: only A's row is visible.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),))
        visible = {
            row[0] for row in conn.execute("SELECT tenant_id FROM phase_transitions")
        }
        assert visible == {tenant_a}

        # Scoped to tenant B: only B's row is visible.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_b),))
        visible = {
            row[0] for row in conn.execute("SELECT tenant_id FROM phase_transitions")
        }
        assert visible == {tenant_b}

        # Attack 1: scoped to A, an explicit read of B's rows returns nothing.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),))
        leaked = conn.execute(
            "SELECT count(*) FROM phase_transitions WHERE tenant_id = %s",
            (tenant_b,),
        ).fetchone()[0]
        assert leaked == 0

        # Attack 2: scoped to A, updating B's rows touches nothing.
        updated = conn.execute(
            "UPDATE phase_transitions SET reason = 'hacked' WHERE tenant_id = %s",
            (tenant_b,),
        ).rowcount
        assert updated == 0

        # Attack 3: scoped to A, deleting B's rows touches nothing.
        deleted = conn.execute(
            "DELETE FROM phase_transitions WHERE tenant_id = %s",
            (tenant_b,),
        ).rowcount
        assert deleted == 0

        # Attack 4: scoped to A, inserting a row for B is rejected by WITH CHECK.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO phase_transitions (tenant_id, to_phase) "
                "VALUES (%s, 'injected')",
                (tenant_b,),
            )


# --- Migration 014: schema hardening (VT-Foundation-fix-1, CL-70 DC2/H3) ------


def test_pipeline_steps_step_seq_unique(migrated):
    """H3: two pipeline_steps rows with the same (run_id, step_seq) are
    rejected by the 014 pipeline_steps_run_step_unique constraint
    (column renamed step_index→step_seq under VT-187 / migration 025)."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Step Seq Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        run_id = str(uuid4())
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_id),
        )
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, status) "
            "VALUES (%s, %s, 0, 'webhook_received', 'completed')",
            (run_id, tenant_id),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO pipeline_steps "
                "(run_id, tenant_id, step_seq, step_kind, status) "
                "VALUES (%s, %s, 0, 'duplicate', 'completed')",
                (run_id, tenant_id),
            )


def test_whatsapp_number_lookup_uses_index(migrated):
    """DC2: the whatsapp_number lookup can use tenants_whatsapp_number_idx
    rather than a sequential scan over tenants."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Discourage seq scan so the planner reveals the index it *can* use —
        # a near-empty table would otherwise always seq-scan regardless.
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                "EXPLAIN SELECT id FROM tenants WHERE whatsapp_number = %s",
                ("+919999900001",),
            )
        )
    assert "tenants_whatsapp_number_idx" in plan, plan
