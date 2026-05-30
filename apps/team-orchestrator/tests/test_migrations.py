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


# --- Migration 044: scheduled_followups (VT-48) -------------------------------


def test_scheduled_followups_idempotency_and_rls(migrated):
    """VT-48: UNIQUE(tenant_id, follow_up_key) idempotency + RLS isolation
    on scheduled_followups (migration 044)."""
    from datetime import datetime, timedelta, timezone

    dsn = migrated["dsn"]
    fire_at = datetime.now(timezone.utc) + timedelta(days=3)

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('SF Tenant A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('SF Tenant B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # Tenant A schedules a follow-up.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),))
        conn.execute(
            "INSERT INTO scheduled_followups "
            "(tenant_id, follow_up_type, follow_up_key, fire_at, payload) "
            "VALUES (%s, 'campaign_followup', 'cfk_1', %s, '{}'::jsonb)",
            (tenant_a, fire_at),
        )

        # Idempotency: same (tenant, key) again → ON CONFLICT DO NOTHING (0 rows).
        inserted = conn.execute(
            "INSERT INTO scheduled_followups "
            "(tenant_id, follow_up_type, follow_up_key, fire_at, payload) "
            "VALUES (%s, 'campaign_followup', 'cfk_1', %s, '{}'::jsonb) "
            "ON CONFLICT (tenant_id, follow_up_key) DO NOTHING",
            (tenant_a, fire_at),
        ).rowcount
        assert inserted == 0

        # Exactly one row visible to A.
        count_a = conn.execute(
            "SELECT count(*) FROM scheduled_followups WHERE follow_up_key = 'cfk_1'"
        ).fetchone()[0]
        assert count_a == 1

        # RLS: tenant B sees none of A's rows.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_b),))
        leaked = conn.execute(
            "SELECT count(*) FROM scheduled_followups WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchone()[0]
        assert leaked == 0

        # B may reuse the SAME follow_up_key (different tenant → different row).
        conn.execute(
            "INSERT INTO scheduled_followups "
            "(tenant_id, follow_up_type, follow_up_key, fire_at, payload) "
            "VALUES (%s, 'campaign_followup', 'cfk_1', %s, '{}'::jsonb)",
            (tenant_b, fire_at),
        )
        count_b = conn.execute(
            "SELECT count(*) FROM scheduled_followups WHERE follow_up_key = 'cfk_1'"
        ).fetchone()[0]
        assert count_b == 1  # only B's row visible under B's scope

        # Attack: scoped to B, inserting for A is rejected by WITH CHECK.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO scheduled_followups "
                "(tenant_id, follow_up_type, follow_up_key, fire_at, payload) "
                "VALUES (%s, 'campaign_followup', 'cfk_attack', %s, '{}'::jsonb)",
                (tenant_a, fire_at),
            )


# --- Migration 045: customers + campaign_recipients cohort integrity (VT-170) -


def test_customers_rls_and_cohort_integrity(migrated):
    """VT-170: customers RLS isolation + campaign_recipients same-tenant
    composite-FK integrity (cross-tenant linkage rejected at the DB)."""
    dsn = migrated["dsn"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Cust Tenant A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Cust Tenant B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]
        cust_a = conn.execute(
            "INSERT INTO customers (tenant_id, display_name) "
            "VALUES (%s, 'Alice A') RETURNING id",
            (tenant_a,),
        ).fetchone()[0]
        cust_b = conn.execute(
            "INSERT INTO customers (tenant_id, display_name) "
            "VALUES (%s, 'Bob B') RETURNING id",
            (tenant_b,),
        ).fetchone()[0]
        run_a = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'campaign', 'running') RETURNING id",
            (tenant_a,),
        ).fetchone()[0]
        camp_a = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, "
            "generated_at) VALUES (%s, %s, '{}'::jsonb, "
            "'proposed', now()) RETURNING id",
            (tenant_a, run_a),
        ).fetchone()[0]

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # RLS: scoped to A, only A's customer is visible.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),))
        visible = {r[0] for r in conn.execute("SELECT id FROM customers")}
        assert cust_a in visible
        assert cust_b not in visible

        # Valid same-tenant linkage succeeds.
        conn.execute(
            "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) "
            "VALUES (%s, %s, %s)",
            (camp_a, cust_a, tenant_a),
        )
        cohort_size = conn.execute(
            "SELECT count(*) FROM campaign_recipients WHERE campaign_id = %s",
            (camp_a,),
        ).fetchone()[0]
        assert cohort_size == 1

        # Cross-tenant linkage: A's campaign + B's customer is REJECTED by
        # the same-tenant composite FK (B's customer is invisible to A and
        # the FK requires matching tenant_id on both sides).
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) "
                "VALUES (%s, %s, %s)",
                (camp_a, cust_b, tenant_a),
            )

    # opt_out_status default + CHECK (superuser, bypasses RLS).
    with psycopg.connect(dsn, autocommit=True) as conn:
        default_status = conn.execute(
            "SELECT opt_out_status FROM customers WHERE id = %s", (cust_a,)
        ).fetchone()[0]
        assert default_status == "subscribed"
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO customers (tenant_id, display_name, opt_out_status) "
                "VALUES (%s, 'X', 'bogus_status')",
                (tenant_a,),
            )


def test_customers_partial_unique_phone(migrated):
    """VT-170: (tenant_id, phone_e164) unique when phone present; NULL ok."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Phone Tenant', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO customers (tenant_id, phone_e164) VALUES (%s, '+919990000001')",
            (tenant,),
        )
        # Duplicate phone same tenant → conflict.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO customers (tenant_id, phone_e164) VALUES (%s, '+919990000001')",
                (tenant,),
            )
    # Multiple NULL phones allowed.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customers (tenant_id, phone_e164) VALUES (%s, NULL)", (tenant,)
        )
        conn.execute(
            "INSERT INTO customers (tenant_id, phone_e164) VALUES (%s, NULL)", (tenant,)
        )
        n = conn.execute(
            "SELECT count(*) FROM customers WHERE tenant_id = %s AND phone_e164 IS NULL",
            (tenant,),
        ).fetchone()[0]
        assert n == 2


# --- Migration 046: operator_allowlist (VT-228) -------------------------------


def test_operator_allowlist_deny_all_rls_and_grant_revoke(migrated):
    """VT-228: operator_allowlist applies; deny-all RLS (rls_tester sees
    nothing even with rows present); grant/revoke semantics."""
    from uuid import uuid4

    dsn = migrated["dsn"]
    op_a = str(uuid4())

    # Service role (superuser, bypasses RLS) can grant + read.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_allowlist (user_id, notes) VALUES (%s, 'vt228 synthetic')",
            (op_a,),
        )
        active = conn.execute(
            "SELECT count(*) FROM operator_allowlist WHERE user_id = %s AND revoked_at IS NULL",
            (op_a,),
        ).fetchone()[0]
        assert active == 1

    # Deny-all RLS: a non-superuser role (rls_tester, created earlier) sees
    # NOTHING — operator_allowlist has FORCE RLS + no policies.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")
        visible = conn.execute("SELECT count(*) FROM operator_allowlist").fetchone()[0]
        assert visible == 0, "deny-all RLS: app/tenant role must see no operators"

    # Revoke (service role): active count → 0, row retained for audit.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE operator_allowlist SET revoked_at = now(), revoke_reason = 'test' "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (op_a,),
        )
        active = conn.execute(
            "SELECT count(*) FROM operator_allowlist WHERE user_id = %s AND revoked_at IS NULL",
            (op_a,),
        ).fetchone()[0]
        assert active == 0
        retained = conn.execute(
            "SELECT count(*) FROM operator_allowlist WHERE user_id = %s",
            (op_a,),
        ).fetchone()[0]
        assert retained == 1, "revoked row kept for audit"


# --- Migration 049: send_idempotency_keys + campaign_messages (VT-44) ----------


def test_send_idempotency_keys_rls_and_unique(migrated):
    """VT-44: send_idempotency_keys UNIQUE(tenant_id, idempotency_key) +
    RLS cross-tenant isolation (migration 049)."""
    dsn = migrated["dsn"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Idem Tenant A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Idem Tenant B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # Tenant A writes a ledger row.
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),)
        )
        conn.execute(
            "INSERT INTO send_idempotency_keys "
            "(tenant_id, idempotency_key, customer_id, message_sid, send_status) "
            "VALUES (%s, 'idem-key-1', NULL, 'SM_test_1', 'sent')",
            (tenant_a,),
        )

        # Idempotency: same (tenant, key) again → ON CONFLICT DO NOTHING (0 rows).
        inserted = conn.execute(
            "INSERT INTO send_idempotency_keys "
            "(tenant_id, idempotency_key, customer_id, message_sid, send_status) "
            "VALUES (%s, 'idem-key-1', NULL, 'SM_test_dup', 'sent') "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING",
            (tenant_a,),
        ).rowcount
        assert inserted == 0, "duplicate idempotency key must be a no-op"

        # Exactly one row visible to A.
        count_a = conn.execute(
            "SELECT count(*) FROM send_idempotency_keys WHERE idempotency_key = 'idem-key-1'"
        ).fetchone()[0]
        assert count_a == 1

        # RLS: tenant B sees none of A's rows.
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_b),)
        )
        leaked = conn.execute(
            "SELECT count(*) FROM send_idempotency_keys WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchone()[0]
        assert leaked == 0, "RLS must block tenant B from seeing tenant A's ledger rows"

        # B may reuse the same idempotency_key (different tenant → different row).
        conn.execute(
            "INSERT INTO send_idempotency_keys "
            "(tenant_id, idempotency_key, customer_id, message_sid, send_status) "
            "VALUES (%s, 'idem-key-1', NULL, 'SM_test_b', 'sent')",
            (tenant_b,),
        )
        count_b = conn.execute(
            "SELECT count(*) FROM send_idempotency_keys WHERE idempotency_key = 'idem-key-1'"
        ).fetchone()[0]
        assert count_b == 1, "only tenant B's row should be visible under B's scope"

        # Attack: scoped to B, inserting for A is rejected by WITH CHECK.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO send_idempotency_keys "
                "(tenant_id, idempotency_key, customer_id, message_sid, send_status) "
                "VALUES (%s, 'attack-key', NULL, 'SM_attack', 'sent')",
                (tenant_a,),
            )


def test_campaign_messages_rls_and_tables_exist(migrated):
    """VT-44: campaign_messages table exists with RLS; cross-tenant isolation."""
    dsn = migrated["dsn"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        # Verify both tables exist.
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        assert "send_idempotency_keys" in tables, "send_idempotency_keys table missing"
        assert "campaign_messages" in tables, "campaign_messages table missing"

        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('CM Tenant A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('CM Tenant B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # Tenant A writes a campaign_message row (freeform send, no campaign_id).
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),)
        )
        conn.execute(
            "INSERT INTO campaign_messages "
            "(tenant_id, customer_id, message_sid, send_status, message_type) "
            "VALUES (%s, NULL, 'SM_cm_test_a', 'sent', 'freeform')",
            (tenant_a,),
        )

        # Tenant B sees none of A's rows.
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_b),)
        )
        leaked = conn.execute(
            "SELECT count(*) FROM campaign_messages WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchone()[0]
        assert leaked == 0, "RLS must block tenant B from seeing tenant A's messages"

        # Attack: scoped to B, inserting for A is rejected by WITH CHECK.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO campaign_messages "
                "(tenant_id, customer_id, message_sid, send_status, message_type) "
                "VALUES (%s, NULL, 'SM_attack', 'sent', 'freeform')",
                (tenant_a,),
            )


def test_campaign_messages_campaign_fk(migrated):
    """VT-44 (Cowork review fix): campaign_messages composite FK
    (tenant_id, campaign_id) -> campaigns(tenant_id, id). MATCH SIMPLE →
    enforced only when campaign_id is set: freeform (NULL) is exempt,
    same-tenant link is allowed, a cross-tenant link is rejected at the DB.
    Superuser conn isolates the FK behaviour from RLS."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        ta = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('FK A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tb = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('FK B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'running') RETURNING id", (ta,)
        ).fetchone()[0]
        camp = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at) "
            "VALUES (%s, %s, '{}'::jsonb, 'sent', now()) RETURNING id", (ta, run)
        ).fetchone()[0]

        # Freeform (NULL campaign_id) — FK exempt.
        conn.execute(
            "INSERT INTO campaign_messages (tenant_id, campaign_id, send_status, "
            "message_type) VALUES (%s, NULL, 'sent', 'freeform')", (ta,)
        )
        # Same-tenant campaign link — allowed.
        conn.execute(
            "INSERT INTO campaign_messages (tenant_id, campaign_id, send_status, "
            "message_type) VALUES (%s, %s, 'template_sent', 'template')", (ta, camp)
        )
        # Cross-tenant: tenant B + tenant A's campaign — FK rejects.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO campaign_messages (tenant_id, campaign_id, send_status, "
                "message_type) VALUES (%s, %s, 'template_sent', 'template')", (tb, camp)
            )
