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
    """The whatsapp_number lookup uses an index, not a seq scan. VT-267 mig 066
    promoted it to the UNIQUE identity index ``tenants_whatsapp_number_key``
    (replacing the non-unique mig-014 ``tenants_whatsapp_number_idx`` per Fazal D1 —
    supersedes CL-76 DC2). Assert the lookup is index-served (name-agnostic)."""
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
    assert "Index Scan" in plan and "whatsapp_number" in plan, plan
    assert "tenants_whatsapp_number_key" in plan, plan  # the VT-267 unique identity index


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


def test_monthly_reports_rls_unique_and_year_month_check(migrated):
    """VT-86 (migration 048): monthly_reports RLS cross-tenant isolation +
    UNIQUE(tenant_id, year_month) + the year_month format CHECK."""
    dsn = migrated["dsn"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Report Tenant A', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Report Tenant B', 'standard', 'paid_active') RETURNING id"
        ).fetchone()[0]
        rep_a = conn.execute(
            "INSERT INTO monthly_reports (tenant_id, year_month, arrr_paise) "
            "VALUES (%s, '2026-04', 500000) RETURNING id",
            (tenant_a,),
        ).fetchone()[0]

        # UNIQUE(tenant_id, year_month): a second 2026-04 for A is rejected.
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO monthly_reports (tenant_id, year_month) "
                "VALUES (%s, '2026-04')",
                (tenant_a,),
            )

        # year_month CHECK: malformed period rejected; fees/net NULLABLE.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO monthly_reports (tenant_id, year_month) "
                "VALUES (%s, '2026-13')",  # month 13 invalid
                (tenant_a,),
            )

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # Scoped to B: A's report is invisible.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant_b),))
        visible = {r[0] for r in conn.execute("SELECT id FROM monthly_reports")}
        assert rep_a not in visible

        # Insert-for-A while scoped to B is rejected by WITH CHECK.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO monthly_reports (tenant_id, year_month) "
                "VALUES (%s, '2026-05')",
                (tenant_a,),
            )


def test_attribution_method_confidence_columns(migrated):
    """VT-240 (migration 047): attributions gains nullable attribution_method
    + attribution_confidence. CHECKs reject bad method / out-of-range
    confidence; a pre-047-shape insert (both omitted) still succeeds."""
    dsn = migrated["dsn"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Attr Tenant', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'campaign', 'running') RETURNING id",
            (tenant,),
        ).fetchone()[0]
        camp = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, "
            "generated_at) VALUES (%s, %s, '{}'::jsonb, 'proposed', now()) "
            "RETURNING id",
            (tenant, run),
        ).fetchone()[0]

        # 1. Populated insert: method + confidence persist + read back.
        attr = conn.execute(
            "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
            "attribution_method, attribution_confidence) "
            "VALUES (%s, %s, 50000, 'exact_match', 0.87) RETURNING id",
            (tenant, camp),
        ).fetchone()[0]
        method, conf = conn.execute(
            "SELECT attribution_method, attribution_confidence "
            "FROM attributions WHERE id = %s",
            (attr,),
        ).fetchone()
        assert method == "exact_match"
        assert conf == pytest.approx(0.87, abs=1e-4)

        # 2. Pre-047-shape insert (both omitted) → NULLs, still valid.
        legacy = conn.execute(
            "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
            "VALUES (%s, %s, 12000) RETURNING id",
            (tenant, camp),
        ).fetchone()[0]
        lmethod, lconf = conn.execute(
            "SELECT attribution_method, attribution_confidence "
            "FROM attributions WHERE id = %s",
            (legacy,),
        ).fetchone()
        assert lmethod is None and lconf is None

        # 3. window_match + manual_owner are accepted by the CHECK.
        for valid_method in ("window_match", "manual_owner"):
            conn.execute(
                "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                "attribution_method) VALUES (%s, %s, 1, %s)",
                (tenant, camp, valid_method),
            )

        # 4. Bad method → CHECK rejects.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                "attribution_method) VALUES (%s, %s, 1, 'bogus_method')",
                (tenant, camp),
            )

        # 5. Out-of-range confidence (>1 and <0) → CHECK rejects.
        for bad_conf in (1.5, -0.1):
            with pytest.raises(psycopg.Error):
                conn.execute(
                    "INSERT INTO attributions (tenant_id, campaign_id, "
                    "attributed_paise, attribution_confidence) "
                    "VALUES (%s, %s, 1, %s)",
                    (tenant, camp, bad_conf),
                )


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


def test_tenant_owner_phone_globally_unique(migrated):
    """VT-250 (D1): owner_phone is a GLOBALLY-unique (E.164) anchor — one
    phone maps to exactly one tenant ACROSS the whole table (not per-tenant).
    NULL owner_phone is allowed on many tenants (partial index)."""
    dsn = migrated["dsn"]

    # Column exists + is nullable TEXT.
    with psycopg.connect(dsn, autocommit=True) as conn:
        col = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'tenants' AND column_name = 'owner_phone'"
        ).fetchone()
    assert col is not None, "owner_phone column must exist"
    assert col[0] == "text"
    assert col[1] == "YES", "owner_phone must be nullable"

    # Randomized per-run E.164 so the test is idempotent across re-runs (the
    # global-unique index would otherwise collide with a prior run's row).
    phone = f"+9198{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        t1 = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, owner_phone) "
            "VALUES ('Owner Phone T1', 'founding', 'onboarding', %s) RETURNING id",
            (phone,),
        ).fetchone()[0]
        assert t1 is not None

        # GLOBAL uniqueness: a DIFFERENT tenant cannot claim the same
        # owner_phone — one phone = one tenant for launch.
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, owner_phone) "
                "VALUES ('Owner Phone T2', 'standard', 'onboarding', %s)",
                (phone,),
            )

    # Partial index: many tenants with NULL owner_phone are allowed.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, owner_phone) "
            "VALUES ('Null Owner A', 'founding', 'onboarding', NULL)"
        )
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, owner_phone) "
            "VALUES ('Null Owner B', 'standard', 'onboarding', NULL)"
        )

    # The unique anchor index exists by name.
    with psycopg.connect(dsn, autocommit=True) as conn:
        idx = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'tenants' "
            "AND indexname = 'idx_tenants_owner_phone_unique'"
        ).fetchone()
    assert idx is not None, "owner_phone unique anchor index must exist"


# --- Migration 051: per-tenant recovery-target config (VT-164) ----------------


def test_tenant_recovery_target_columns(migrated):
    """VT-164 (migration 051): tenants gains recovery_target_multiplier (NUMERIC)
    + recovery_target_floor_paise (BIGINT) with correct DEFAULTs and CHECKs."""
    dsn = migrated["dsn"]

    # 1. Columns exist with the right types.
    with psycopg.connect(dsn, autocommit=True) as conn:
        cols = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'tenants' "
                "AND column_name IN ('recovery_target_multiplier', 'recovery_target_floor_paise')"
            )
        }
    assert "recovery_target_multiplier" in cols, "recovery_target_multiplier column missing"
    assert "recovery_target_floor_paise" in cols, "recovery_target_floor_paise column missing"
    assert cols["recovery_target_multiplier"] == "numeric", (
        f"multiplier type should be numeric, got {cols['recovery_target_multiplier']!r}"
    )
    assert cols["recovery_target_floor_paise"] in ("bigint", "integer"), (
        f"floor_paise type should be bigint/integer, got {cols['recovery_target_floor_paise']!r}"
    )

    with psycopg.connect(dsn, autocommit=True) as conn:
        # 2. DEFAULT backfill: a freshly-inserted tenant gets the expected defaults.
        tenant_id = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-164 Default Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT recovery_target_multiplier, recovery_target_floor_paise "
            "FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
        assert float(row[0]) == 1.1, f"default multiplier should be 1.1, got {row[0]!r}"
        assert int(row[1]) == 50000, f"default floor should be 50000, got {row[1]!r}"

        # 3. Override persists correctly.
        conn.execute(
            "UPDATE tenants SET recovery_target_multiplier = 1.5, "
            "recovery_target_floor_paise = 100000 WHERE id = %s",
            (tenant_id,),
        )
        updated = conn.execute(
            "SELECT recovery_target_multiplier, recovery_target_floor_paise "
            "FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
        assert float(updated[0]) == 1.5
        assert int(updated[1]) == 100000

        # 4. CHECK rejects multiplier <= 0.
        for bad_mul in (0, -1):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE tenants SET recovery_target_multiplier = %s WHERE id = %s",
                    (bad_mul, tenant_id),
                )

        # 5. CHECK rejects floor_paise < 0.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE tenants SET recovery_target_floor_paise = -1 WHERE id = %s",
                (tenant_id,),
            )

        # 6. Zero floor is allowed (CHECK is >=0).
        conn.execute(
            "UPDATE tenants SET recovery_target_floor_paise = 0 WHERE id = %s",
            (tenant_id,),
        )
        zero_floor = conn.execute(
            "SELECT recovery_target_floor_paise FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()[0]
        assert int(zero_floor) == 0


# --- Migration 052: pending_approvals + pipeline_runs 'paused' (VT-47) --------


def test_pipeline_runs_status_accepts_paused(migrated):
    """VT-47 (migration 052): the pipeline_runs.status CHECK is altered to
    accept the NEW 'paused' terminal; a bogus value is still rejected."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('Paused Status Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]

        # 'paused' is accepted.
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'paused') RETURNING id",
            (tenant,),
        ).fetchone()[0]
        assert run is not None

        # A run can transition paused -> completed (resume / timeout path).
        conn.execute(
            "UPDATE pipeline_runs SET status = 'completed' WHERE id = %s", (run,)
        )

        # An unknown status is still rejected by the (re-added) CHECK.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                "VALUES (%s, 'orchestrator', 'bogus_status')",
                (tenant,),
            )

    # The constraint exists by its canonical (auto-)name and lists 'paused'.
    with psycopg.connect(dsn, autocommit=True) as conn:
        condef = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'pipeline_runs'::regclass "
            "AND conname = 'pipeline_runs_status_check'"
        ).fetchone()
    assert condef is not None, "pipeline_runs_status_check must exist after 052"
    assert "paused" in condef[0]


def test_pending_approvals_rls_and_decision_check(migrated):
    """VT-47 (migration 052): pending_approvals RLS cross-tenant isolation +
    the decision CHECK rejects an unknown verb; a pending row (decision NULL)
    is valid."""
    dsn = migrated["dsn"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_a = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('PA Tenant A', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        tenant_b = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('PA Tenant B', 'standard', 'onboarding') RETURNING id"
        ).fetchone()[0]
        run_a = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'paused') RETURNING id",
            (tenant_a,),
        ).fetchone()[0]

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_tester")

        # Tenant A writes a pending approval (decision NULL).
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_a),)
        )
        conn.execute(
            "INSERT INTO pending_approvals "
            "(tenant_id, run_id, approval_type, summary, timeout_at) "
            "VALUES (%s, %s, 'campaign_send', 'approve?', now() + interval '48 hours')",
            (tenant_a, run_a),
        )
        count_a = conn.execute(
            "SELECT count(*) FROM pending_approvals WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchone()[0]
        assert count_a == 1

        # RLS: tenant B sees none of A's approvals.
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_b),)
        )
        leaked = conn.execute(
            "SELECT count(*) FROM pending_approvals WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchone()[0]
        assert leaked == 0, "RLS must block tenant B from seeing tenant A's approvals"

        # Attack: scoped to B, inserting for A is rejected by WITH CHECK.
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO pending_approvals "
                "(tenant_id, run_id, approval_type, summary, timeout_at) "
                "VALUES (%s, %s, 'campaign_send', 'x', now() + interval '1 hour')",
                (tenant_a, run_a),
            )

    # decision CHECK: a bogus verb is rejected (superuser, bypasses RLS).
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO pending_approvals "
                "(tenant_id, run_id, approval_type, summary, timeout_at, decision) "
                "VALUES (%s, %s, 'campaign_send', 'x', now() + interval '1 hour', 'bogus')",
                (tenant_a, run_a),
            )
        # The valid verbs are accepted.
        for verb in ("approved", "rejected", "needs_changes", "timeout"):
            conn.execute(
                "INSERT INTO pending_approvals "
                "(tenant_id, run_id, approval_type, summary, timeout_at, decision, "
                " status, resolved_at) "
                "VALUES (%s, %s, 'campaign_send', 'x', now(), %s, 'approved', now())",
                (tenant_a, run_a, verb),
            )


def test_pending_approvals_run_fk_same_tenant(migrated):
    """VT-47: pending_approvals.run_id FK to pipeline_runs — a run that does
    not exist is rejected (referential integrity for the resume key)."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('PA FK Tenant', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO pending_approvals "
                "(tenant_id, run_id, approval_type, summary, timeout_at) "
                "VALUES (%s, %s, 'campaign_send', 'x', now() + interval '1 hour')",
                (tenant, str(uuid4())),  # non-existent run_id
            )


def test_create_role_guarded_and_idempotent(migrated):
    """VT-271: CREATE ROLE in 015/027 is guarded (DO-block IF NOT EXISTS pg_roles), so a fresh-DB
    apply on a cluster where the roles already exist (roles are cluster-global; the runner's
    applied-tracking is per-DB) is a NO-OP instead of halting with 'role already exists'. That
    removes the need for the pre-push hook's drop-roles workaround.

    (a) regression-lock: both files carry the guard (a future bare CREATE ROLE fails here).
    (b) the guard is genuinely idempotent against an already-existing role.
    Scope is the ROLE creation only — not full-file re-runnability (other DDL like CREATE POLICY
    is intentionally not idempotent)."""
    role_files = [
        p for p in apply_migrations.migration_files() if p.name.startswith(("015_", "027_"))
    ]
    assert len(role_files) == 2, [p.name for p in role_files]
    for p in role_files:
        src = p.read_text(encoding="utf-8")
        assert "pg_roles WHERE rolname" in src, f"{p.name}: CREATE ROLE not guarded"
        assert "CREATE ROLE" in src

    dsn = migrated["dsn"]  # roles already created by the fixture
    with psycopg.connect(dsn, autocommit=True) as conn:
        for role, opts in (("app_role", "NOLOGIN"), ("app_operator_role", "NOLOGIN INHERIT")):
            # The exact guard form must be a no-op against the existing role (no DuplicateObject).
            conn.execute(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
                f"THEN CREATE ROLE {role} {opts}; END IF; END $$;"
            )
            n = conn.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()[0]
            assert n == 1, f"{role}: expected 1, got {n}"


# --- Migration 165: VT-605 plan-store columns (manager_tasks / manager_task_steps) --------------


def test_manager_tasks_plan_store_columns_exist_with_safe_defaults(migrated):
    """VT-605 (mig 165): manager_tasks gains plan_revision / terminal_outcome /
    owner_notification_status. A pre-165-shape insert (all three omitted) still succeeds with the
    documented defaults — no backfill needed, every existing caller is unaffected."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT605 Cols Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        task = conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective) VALUES (%s, '{}'::jsonb) RETURNING id",
            (tenant,),
        ).fetchone()[0]
        row = conn.execute(
            "SELECT plan_revision, terminal_outcome, owner_notification_status "
            "FROM manager_tasks WHERE id = %s",
            (task,),
        ).fetchone()
    assert row[0] == 1
    assert row[1] is None
    assert row[2] == "not_required"


def test_manager_tasks_status_accepts_queued(migrated):
    """VT-605 (mig 165): the status CHECK is extended to accept 'queued'; a bogus value is still
    rejected."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT605 Queued Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        task = conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective, status) "
            "VALUES (%s, '{}'::jsonb, 'queued') RETURNING id",
            (tenant,),
        ).fetchone()[0]
        assert task is not None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO manager_tasks (tenant_id, objective, status) "
                "VALUES (%s, '{}'::jsonb, 'bogus_status')",
                (tenant,),
            )


def test_manager_tasks_terminal_outcome_and_owner_notification_status_checks(migrated):
    """VT-605 (mig 165): terminal_outcome accepts NULL + its 5 named values, rejects anything else;
    owner_notification_status accepts its 5 named values, rejects anything else."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT605 Outcome Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]

        for outcome in (
            "completed_with_effect", "completed_no_action", "failed", "escalated", "cancelled",
        ):
            conn.execute(
                "INSERT INTO manager_tasks (tenant_id, objective, terminal_outcome) "
                "VALUES (%s, '{}'::jsonb, %s)",
                (tenant, outcome),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO manager_tasks (tenant_id, objective, terminal_outcome) "
                "VALUES (%s, '{}'::jsonb, 'bogus_outcome')",
                (tenant,),
            )

        for status in ("not_required", "pending", "accepted", "delivered", "failed"):
            conn.execute(
                "INSERT INTO manager_tasks (tenant_id, objective, owner_notification_status) "
                "VALUES (%s, '{}'::jsonb, %s)",
                (tenant, status),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO manager_tasks (tenant_id, objective, owner_notification_status) "
                "VALUES (%s, '{}'::jsonb, 'bogus_notification_status')",
                (tenant,),
            )


def test_manager_task_steps_plan_store_columns_and_checks(migrated):
    """VT-605 (mig 165): manager_task_steps gains plan_revision (default 1) + specialist
    (nullable, CHECKed to the 3 roster specialists); kind admits 'advisory_tool'; evidence_kind
    admits 'pipeline_step'; status admits 'superseded'."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT605 Steps Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        task = conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective) VALUES (%s, '{}'::jsonb) RETURNING id",
            (tenant,),
        ).fetchone()[0]

        # pre-165-shape insert (plan_revision/specialist omitted) still succeeds with defaults.
        step = conn.execute(
            "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, kind) "
            "VALUES (%s, %s, 1, 'effect') RETURNING id",
            (tenant, task),
        ).fetchone()[0]
        row = conn.execute(
            "SELECT plan_revision, specialist FROM manager_task_steps WHERE id = %s", (step,)
        ).fetchone()
        assert row[0] == 1
        assert row[1] is None

        # kind admits 'advisory_tool'.
        conn.execute(
            "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, kind) "
            "VALUES (%s, %s, 2, 'advisory_tool')",
            (tenant, task),
        )
        # specialist admits the 3 roster values, on a NEW task (avoids the (task, revision, seq)
        # unique index — a fresh task per specialist keeps this test about the CHECK, not the index).
        for specialist in ("onboarding_conductor", "integration_agent", "sales_recovery_agent"):
            t2 = conn.execute(
                "INSERT INTO manager_tasks (tenant_id, objective) VALUES (%s, '{}'::jsonb) RETURNING id",
                (tenant,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, kind, specialist) "
                "VALUES (%s, %s, 1, 'specialist_dispatch', %s)",
                (tenant, t2, specialist),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, kind, specialist) "
                "VALUES (%s, %s, 99, 'specialist_dispatch', 'bogus_specialist')",
                (tenant, task),
            )
        # evidence_kind admits 'pipeline_step'.
        conn.execute(
            "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, kind, evidence_kind) "
            "VALUES (%s, %s, 3, 'verification', 'pipeline_step')",
            (tenant, task),
        )
        # status admits 'superseded'.
        conn.execute(
            "UPDATE manager_task_steps SET status = 'superseded' WHERE id = %s", (step,)
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE manager_task_steps SET status = 'bogus_status' WHERE id = %s", (step,)
            )


def test_manager_task_steps_seq_unique_within_revision_not_across(migrated):
    """VT-605 (mig 165): the (task_id, step_seq) unique index (mig 152) is replaced by
    (task_id, plan_revision, step_seq) — a revision may legitimately reuse step_seq=1 under a NEW
    plan_revision (that is exactly what plan_store.revise_plan does); the SAME step_seq within the
    SAME revision is still rejected."""
    dsn = migrated["dsn"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT605 Seq Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0]
        task = conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective) VALUES (%s, '{}'::jsonb) RETURNING id",
            (tenant,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, plan_revision, kind) "
            "VALUES (%s, %s, 1, 1, 'effect')",
            (tenant, task),
        )
        # Same step_seq, SAME revision → rejected.
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, plan_revision, kind) "
                "VALUES (%s, %s, 1, 1, 'effect')",
                (tenant, task),
            )
        # Same step_seq, NEW revision → allowed (a revision reusing step_seq=1).
        conn.execute(
            "INSERT INTO manager_task_steps (tenant_id, task_id, step_seq, plan_revision, kind) "
            "VALUES (%s, %s, 1, 2, 'effect')",
            (tenant, task),
        )
        n = conn.execute(
            "SELECT count(*) FROM manager_task_steps WHERE task_id = %s", (task,)
        ).fetchone()[0]
        assert n == 2
