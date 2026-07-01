"""DSR purge — tenant-wide subject data deletion (VT-DSR-Purge).

DB-substrate tests against a real Postgres. Requires ``DATABASE_URL``
+ the dbos stack; runs in the CI ``orchestrator`` job which provisions
``pgvector/pgvector:pg16``.

Critical invariant under test: cross-tenant isolation. Two tenants are
seeded with parallel data across every inventoried table; the purge
runs against tenant A; tenant B's rows MUST be untouched.

Other invariants:
  - The DSR ticket flips to status='completed' on success.
  - A second purge call on the same ticket is a safe idempotent no-op.
  - privacy_audit_log retention is preserved + a purge event is appended.
  - The DBOS layer is NOT synchronously deleted (documented: PR #47's
    time-based purge handles DBOS subject data on the ~2h cadence).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-DSR-Purge substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the pool exists."""
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


# --- Seeding helpers --------------------------------------------------------


def _new_tenant(dsn: str, *, name: str = "DSR purge test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            # VT-160: seed every identifying column so the anonymize assertions
            # have real PII to scrub (owner_phone is UNIQUE-indexed → keep it
            # unique per tenant; locality + owner_contact carry subject PII).
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "whatsapp_number, owner_phone, owner_contact, locality) "
            "VALUES (%s, 'founding', 'paid_active', %s, %s, %s, %s) "
            "RETURNING id",
            (
                name,
                f"+9199{uuid4().int % 10**8:08d}",
                f"+9188{uuid4().int % 10**8:08d}",
                "Owner Contact PII",
                "Andheri West",
            ),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _open_dsr_ticket(dsn: str, tenant_id: UUID) -> UUID:
    """Open a deletion DSR ticket via superuser (RLS bypassed at seed
    time; the production-role purge path runs under RLS)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, "
            "acknowledged_at) VALUES (%s, 'deletion', 'acknowledged', now()) "
            "RETURNING id",
            (str(tenant_id),),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_full_tenant_data(dsn: str, tenant_id: UUID) -> dict[str, UUID]:
    """Populate at least one row in every inventoried purgeable table
    for ``tenant_id``. Returns a dict of seeded row ids for later
    assertions (subset — not every table needs an id round-trip)."""
    ids: dict[str, UUID] = {}
    run_id = uuid4()

    with psycopg.connect(dsn, autocommit=True) as conn:
        # pipeline_runs (parent of pipeline_steps / campaigns / owner_inputs)
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (str(run_id), str(tenant_id)),
        )
        ids["pipeline_runs"] = run_id

        # pipeline_steps
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, input_envelope, status) "
            "VALUES (%s, %s, 0, 'webhook_received', '{}'::jsonb, 'completed')",
            (str(run_id), str(tenant_id)),
        )

        # phase_transitions
        conn.execute(
            "INSERT INTO phase_transitions (tenant_id, from_phase, "
            "to_phase, reason) VALUES (%s, 'onboarding', 'trial', 'signup')",
            (str(tenant_id),),
        )

        # subscriptions (schema: tenant_id, status, no plan_tier column)
        conn.execute(
            "INSERT INTO subscriptions (tenant_id, status) "
            "VALUES (%s, 'active')",
            (str(tenant_id),),
        )

        # phone_token_resolutions (PK is ``phone_token``; schema columns:
        # phone_token, tenant_id, phone_number_encrypted, resolved_count,
        # last_accessed_at, created_at, customer_id) — columns renamed
        # under VT-187 / migration 025.
        conn.execute(
            "INSERT INTO phone_token_resolutions (phone_token, tenant_id, "
            "phone_number_encrypted, last_accessed_at) "
            "VALUES (%s, %s, %s, now())",
            (
                f"phone_tok_{uuid4().hex[:16]}",
                str(tenant_id),
                "encrypted-blob-placeholder",
            ),
        )

        # twilio_inbound_events
        conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s)",
            (f"SM{uuid4().hex}", str(tenant_id)),
        )

        # rate_limit_buckets
        conn.execute(
            "INSERT INTO rate_limit_buckets (tenant_id, window_start, "
            "count) VALUES (%s, date_trunc('minute', now()), 1)",
            (str(tenant_id),),
        )

        # subscriber_states
        conn.execute(
            "INSERT INTO subscriber_states (tenant_id, phase, "
            "last_campaign_at) VALUES (%s, 'paid_active', now())",
            (str(tenant_id),),
        )

        # tenant_oauth_tokens (VT-422 GAP-1): the per-(tenant, connector) ENCRYPTED OAuth
        # credential. FK tenants but the tenant row is anonymized (not deleted) on DSR, so
        # the FK never CASCADEs → the purge MUST sweep it explicitly or the encrypted token
        # survives erasure (the DPDP-erasure bug VT-422 closes). PK is (tenant_id, connector_id).
        conn.execute(
            "INSERT INTO tenant_oauth_tokens "
            "(tenant_id, connector_id, refresh_token_encrypted, scopes, "
            " push_secret, shop_url) "
            "VALUES (%s, 'shopify', 'enc-blob-placeholder', "
            "ARRAY['read_orders','write_orders'], 'whsec_dsr_seed', "
            "'dsr-seed.myshopify.com')",
            (str(tenant_id),),
        )

        # campaigns (post-018 reshape: plan_json JSONB)
        conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, status, "
            "generated_at, plan_json) "
            "VALUES (%s, %s, 'proposed', now(), '{}'::jsonb)",
            (str(tenant_id), str(run_id)),
        )

        # l1_entities + l1_relationships
        e1 = uuid4()
        e2 = uuid4()
        conn.execute(
            "INSERT INTO l1_entities (id, tenant_id, entity_type) "
            "VALUES (%s, %s, 'customer'), (%s, %s, 'customer')",
            (str(e1), str(tenant_id), str(e2), str(tenant_id)),
        )
        ids["l1_entity_a"] = e1
        ids["l1_entity_b"] = e2
        conn.execute(
            "INSERT INTO l1_relationships (tenant_id, from_entity, "
            "to_entity, relationship_type) VALUES (%s, %s, %s, 'knows')",
            (str(tenant_id), str(e1), str(e2)),
        )

        # owner_inputs (exists on main post-#47 squash)
        conn.execute(
            "INSERT INTO owner_inputs (tenant_id, run_id, message_sid, "
            "intent, segment, occasion) "
            "VALUES (%s, %s, %s, 'winback', 'dormant', 'diwali')",
            (str(tenant_id), str(run_id), f"SM{uuid4().hex}"),
        )

        # episodic_events (VT-323): L2 row with PII-bearing payload that MUST be
        # hard-deleted by the purge (referenced_entity_type/id model the agent
        # acting on a customer — VT-320).
        conn.execute(
            "INSERT INTO episodic_events "
            "(tenant_id, event_type, summary, payload, referenced_entity_type, "
            "referenced_entity_id, occurred_at) "
            "VALUES (%s, 'customer_action_taken', 'dsr-seed', "
            "'{\"action\": \"campaign_send\"}'::jsonb, 'customer', %s, now())",
            (str(tenant_id), str(uuid4())),
        )

        # platform_listings (VT-325): per-listing source. FK tenants ON DELETE
        # CASCADE does NOT fire on a DSR (tenant is anonymized, not deleted), so the
        # purge MUST hard-delete it here — the VT-323 lesson on a fresh table.
        conn.execute(
            "INSERT INTO platform_listings "
            "(tenant_id, platform, external_listing_id, rating, attributes) "
            "VALUES (%s, 'swiggy', 'dsr-seed-rest', 4.0, "
            "'{\"cuisines\": [\"x\"]}'::jsonb)",
            (str(tenant_id),),
        )

        # kg_events + kg_events_processed (VT-327): the KG transactional outbox carries the
        # TENANT_CREATED business_name (owner PII) in payload AT REST (the drain only stamps
        # drained_at, never deletes) → MUST hard-delete on DSR. The per-tenant marker lets the
        # completeness test scan for surviving PII post-purge.
        # event_type lowercase to match the real emitter (kg_vocab: 'tenant_created').
        conn.execute(
            "INSERT INTO kg_events (event_type, tenant_id, payload) "
            "VALUES ('tenant_created', %s, %s::jsonb)",
            (str(tenant_id), '{"business_name": "kgpii-' + str(tenant_id) + '"}'),
        )
        conn.execute(
            "INSERT INTO kg_events_processed (event_id, event_type, tenant_id, status) "
            "VALUES (%s, 'tenant_created', %s, 'processed')",
            (str(uuid4()), str(tenant_id)),
        )
        # The kg_population projection copies business_name into the l1_entities
        # business_profile (attributes JSONB) — seed the SAME marker there so the
        # cross-table completeness scan exercises a second PII surface (both purged).
        conn.execute(
            "INSERT INTO l1_entities (id, tenant_id, entity_type, attributes) "
            "VALUES (%s, %s, 'business_profile', %s::jsonb)",
            (str(uuid4()), str(tenant_id), '{"business_name": "kgpii-' + str(tenant_id) + '"}'),
        )

        # consent_records (VT-82): owner DPDPA consent proof. RETAINED on DSR (NOT in
        # _PURGE_ORDER) like privacy_audit_log — PII-free proof of lawful processing.
        conn.execute(
            "INSERT INTO consent_records "
            "(tenant_id, consent_dpdpa, consent_residency, dpdpa_version, residency_version) "
            "VALUES (%s, true, true, 'dpdpa_v1_2026-06', 'residency_v1_2026-06')",
            (str(tenant_id),),
        )

        # tm_audit_log (VT-514, mig 147) + debug_events (VT-515, mig 146): VT-518 — the two
        # tenant-scoped PII-bearing observability tables. Both MUST be hard-deleted on DSR
        # (redact-at-write + RLS is insufficient for erasure — redacted activity history is
        # still the subject's data). Seed one row each so the broad sweep + cross-tenant
        # assertions exercise them. tm_audit_log run_id/parent_audit_id left NULL (FK-free
        # seed); debug_events tenant_id is set (the subject's row — NULL-tenant pre-tenant
        # failures are a separate, non-subject class the purge correctly leaves alone).
        conn.execute(
            "INSERT INTO tm_audit_log "
            "(tenant_id, event_layer, event_kind, actor, summary) "
            "VALUES (%s, 'does', 'business_action', 'sales_recovery', 'dsr-seed')",
            (str(tenant_id),),
        )
        conn.execute(
            "INSERT INTO debug_events "
            "(tenant_id, failure_type, component, severity) "
            "VALUES (%s, 'exception', 'signup', 'error')",
            (str(tenant_id),),
        )
        # VT-524: owner_notifications delivery ledger — tenant data, must be erased on DSR.
        conn.execute(
            "INSERT INTO owner_notifications "
            "(tenant_id, template_name, message_sid, owner_notification_status) "
            "VALUES (%s, 'team_welcome3', 'SMtest-dsr-seed', 'accepted')",
            (str(tenant_id),),
        )
        # VT-525: manager_tasks + manager_task_steps — task spine, tenant data, erased on DSR.
        mtask = conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective, status) "
            "VALUES (%s, '{\"goal\": \"dsr-seed\"}'::jsonb, 'running') RETURNING id",
            (str(tenant_id),),
        ).fetchone()
        mtask_id = mtask["id"] if isinstance(mtask, dict) else mtask[0]
        conn.execute(
            "INSERT INTO manager_task_steps "
            "(tenant_id, task_id, step_seq, kind, status) "
            "VALUES (%s, %s, 1, 'specialist_dispatch', 'pending')",
            (str(tenant_id), str(mtask_id)),
        )
        # VT-527: pending_questions — owner-clarification ledger, tenant data, erased on DSR.
        conn.execute(
            "INSERT INTO pending_questions "
            "(tenant_id, task_id, question_kind, question_text, status) "
            "VALUES (%s, %s, 'clarification', 'which cohort?', 'open')",
            (str(tenant_id), str(mtask_id)),
        )
        # VT-531: agent_corrections — reviewer-correction store, tenant data, erased on DSR.
        conn.execute(
            "INSERT INTO agent_corrections "
            "(tenant_id, agent, correction_kind, decision_verb, correction_text) "
            "VALUES (%s, 'sales_recovery', 'edit', 'needs_changes', 'make it shorter')",
            (str(tenant_id),),
        )
        # VT-550: agent_memory — TENANT-scoped learnable memory, tenant data, erased on DSR.
        conn.execute(
            "INSERT INTO agent_memory "
            "(tenant_id, memory_scope, source, memory_key, content) "
            "VALUES (%s, 'tenant', 'learned', 'tone_pref', 'owner likes short warm messages')",
            (str(tenant_id),),
        )
        # VT-552: incidents — durable incident records, tenant data, erased on DSR.
        conn.execute(
            "INSERT INTO incidents (tenant_id, incident_kind, severity) "
            "VALUES (%s, 'silent_terminal', 'warning')",
            (str(tenant_id),),
        )

        # privacy_audit_log — pre-existing event, MUST survive purge. VT-80:
        # write through the real hash-chain writer (a seeded event_type that is
        # NOT one of the purge events, so the purge-count assertions stay exact).
        from orchestrator.observability.audit_log import log_privacy_event

        log_privacy_event(
            conn,
            tenant_id=tenant_id,
            event_type="phone_token_resolved",
            payload={"note": "pre-purge fixture event"},
            actor="test",
        )

    return ids


def _count_tenant_rows(dsn: str, table: str, tenant_id: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _tenant_row(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    """Default psycopg row_factory returns tuples — index positionally."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT business_name, whatsapp_number, opt_out, "
            "owner_phone, owner_contact, locality FROM tenants "
            "WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "business_name": row[0],
        "whatsapp_number": row[1],
        "opt_out": row[2],
        "owner_phone": row[3],
        "owner_contact": row[4],
        "locality": row[5],
    }


def _ticket_row(dsn: str, ticket_id: UUID) -> dict[str, Any]:
    """Default psycopg row_factory returns tuples — index positionally."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, completed_at FROM dsr_tickets WHERE id = %s",
            (str(ticket_id),),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "completed_at": row[1]}


# --- Tests ------------------------------------------------------------------


# Tables that should be ZERO for the purged tenant after the sweep.
_PURGED_TABLES = (
    "l1_relationships",
    "l1_entities",
    "episodic_events",  # VT-323: L2 episodic memory must be swept by DSR purge
    "platform_listings",  # VT-325: per-listing source must be swept by DSR purge
    "kg_events_processed",  # VT-327: KG consumer ledger must be swept by DSR purge
    "kg_events",  # VT-327: KG outbox (TENANT_CREATED business_name PII) must be swept
    "tm_audit_log",  # VT-518: TM audit/trace (VT-514) — tenant PII activity history, erased on DSR
    "debug_events",  # VT-518: debug/failure log (VT-515) — tenant PII, erased on DSR
    "owner_notifications",  # VT-524: owner-notification delivery ledger — tenant data, erased on DSR
    "manager_task_steps",  # VT-525: task step plan — tenant data, erased on DSR
    "manager_tasks",  # VT-525: task spine — tenant data, erased on DSR
    "pending_questions",  # VT-527: owner-clarification ledger — tenant data, erased on DSR
    "agent_corrections",  # VT-531: reviewer-correction store — tenant data, erased on DSR
    "agent_memory",  # VT-550: tenant learnable memory — tenant data, erased on DSR (global seeds survive)
    "incidents",  # VT-552: durable incident records — tenant data, erased on DSR
    "owner_inputs",
    "campaigns",
    "pipeline_steps",
    "pipeline_runs",
    "subscriber_states",
    "phase_transitions",
    "subscriptions",
    "phone_token_resolutions",
    "tenant_oauth_tokens",  # VT-422 GAP-1: encrypted OAuth credential must be erased on DSR
    "twilio_inbound_events",
    "rate_limit_buckets",
)


def test_purge_clears_subject_data_across_all_inventoried_tables(substrate):  # type: ignore[no-untyped-def]
    """Tenant A is purged; every inventoried purgeable table reports
    zero rows for tenant A. The completion tombstone (dsr_tickets +
    anonymized tenants row + privacy_audit_log) is present."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_a = _new_tenant(substrate.dsn, name="Tenant A")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    ticket_a = _open_dsr_ticket(substrate.dsn, tenant_a)

    # Pre-purge sanity — all tables have at least 1 row for A.
    for table in _PURGED_TABLES:
        assert _count_tenant_rows(substrate.dsn, table, tenant_a) >= 1, (
            f"fixture broken: {table} not seeded for tenant A"
        )

    result = purge_tenant_data(ticket_a)

    assert result.tenant_id == tenant_a
    assert result.ticket_id == ticket_a
    assert result.already_completed is False
    assert result.tenant_anonymized is True

    # Post-purge: every purgeable table is empty for A.
    for table in _PURGED_TABLES:
        remaining = _count_tenant_rows(substrate.dsn, table, tenant_a)
        assert remaining == 0, (
            f"DSR purge left {remaining} row(s) in {table} for tenant A"
        )

    # tenants row kept, anonymized.
    tenant_row = _tenant_row(substrate.dsn, tenant_a)
    assert tenant_row is not None, "tenants row hard-deleted — tombstone gone"
    assert tenant_row["business_name"] == "[deleted]"
    assert tenant_row["whatsapp_number"] is None
    assert tenant_row["opt_out"] is True

    # Ticket flipped to completed with timestamp.
    ticket_row = _ticket_row(substrate.dsn, ticket_a)
    assert ticket_row["status"] == "completed"
    assert isinstance(ticket_row["completed_at"], datetime)


# Every purgeable surface the TENANT_CREATED business_name could land in, plus the retained
# privacy_audit_log + the tombstoned tenants row. The completeness scan iterates ALL of them so
# a future PII-bearing table can't silently slip the purge-order.
_PII_SCAN_TABLES = (*_PURGED_TABLES, "privacy_audit_log", "tenants")


def _marker_rows_anywhere(dsn: str, table: str, marker: str) -> int:
    """Rows in `table` whose full text rendering contains the marker — catches the marker in
    ANY column (JSONB or text), not just a named one. `table` is from a fixed allowlist."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} t WHERE t::text LIKE %s",  # noqa: S608 — allowlist
            (f"%{marker}%",),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_purge_leaves_no_tenant_created_pii_at_payload_level(substrate):  # type: ignore[no-untyped-def]
    """VT-327 COMPLETENESS (CROSS-TABLE): the TENANT_CREATED business_name marker — seeded in
    BOTH kg_events.payload AND the l1_entities business_profile (the kg_population projection) —
    survives in NO purgeable surface after a tenant DSR. A real cross-table scan (not a
    single-table count), so a future PII-bearing table can't silently slip the purge-order.
    Cross-tenant: the co-resident's PII is untouched."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_a = _new_tenant(substrate.dsn, name="Tenant A")
    tenant_b = _new_tenant(substrate.dsn, name="Tenant B")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    _seed_full_tenant_data(substrate.dsn, tenant_b)
    ticket_a = _open_dsr_ticket(substrate.dsn, tenant_a)

    marker_a, marker_b = f"kgpii-{tenant_a}", f"kgpii-{tenant_b}"
    # fixture: A's marker is present in >= 2 surfaces (kg_events + l1_entities) pre-purge.
    pre_a = sum(_marker_rows_anywhere(substrate.dsn, t, marker_a) for t in _PII_SCAN_TABLES)
    assert pre_a >= 2, f"fixture broken: A's PII marker seeded in only {pre_a} surface(s)"

    purge_tenant_data(ticket_a)

    # A's business_name survives in NO purgeable surface (the real cross-table guard)...
    for table in _PII_SCAN_TABLES:
        assert _marker_rows_anywhere(substrate.dsn, table, marker_a) == 0, (
            f"TENANT_CREATED business_name (owner PII) survived the DSR purge in {table}"
        )
    # ...and the co-resident tenant B's PII is untouched (cross-tenant isolation).
    post_b = sum(_marker_rows_anywhere(substrate.dsn, t, marker_b) for t in _PII_SCAN_TABLES)
    assert post_b >= 2, "co-resident tenant B's PII was wrongly purged"


def test_purge_preserves_other_tenant_data(substrate):  # type: ignore[no-untyped-def]
    """Cross-tenant isolation: purging tenant A does NOT touch
    tenant B's rows. THE compliance-critical invariant."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_a = _new_tenant(substrate.dsn, name="Tenant A (purgee)")
    tenant_b = _new_tenant(substrate.dsn, name="Tenant B (untouched)")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    _seed_full_tenant_data(substrate.dsn, tenant_b)
    ticket_a = _open_dsr_ticket(substrate.dsn, tenant_a)

    purge_tenant_data(ticket_a)

    # Tenant B's rows survive in every table.
    for table in _PURGED_TABLES:
        remaining = _count_tenant_rows(substrate.dsn, table, tenant_b)
        assert remaining >= 1, (
            f"cross-tenant leak: purging tenant A wiped tenant B's "
            f"{table} row(s) — remaining count {remaining}"
        )

    # Tenant B's tenants row unchanged (business_name intact).
    b_row = _tenant_row(substrate.dsn, tenant_b)
    assert b_row is not None
    assert b_row["business_name"] == "Tenant B (untouched)"
    assert b_row["whatsapp_number"] is not None
    assert b_row["opt_out"] is False


def test_purge_retains_owner_consent_records(substrate):  # type: ignore[no-untyped-def]
    """VT-82: consent_records (owner DPDPA/residency consent proof) is RETAINED on a
    tenant DSR — deliberately NOT in _PURGE_ORDER, like privacy_audit_log. It is
    PII-free (tenant_id + booleans + version strings + ts) and is legal proof of
    lawful processing. The purged tenant's row survives; a co-resident is untouched."""
    from orchestrator.dsr_purge import _PURGE_ORDER, purge_tenant_data

    assert "consent_records" not in _PURGE_ORDER, "consent_records must NOT be purged"

    tenant_a = _new_tenant(substrate.dsn, name="Consent retention A")
    tenant_b = _new_tenant(substrate.dsn, name="Consent retention B")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    _seed_full_tenant_data(substrate.dsn, tenant_b)
    ticket_a = _open_dsr_ticket(substrate.dsn, tenant_a)

    before_a = _count_tenant_rows(substrate.dsn, "consent_records", tenant_a)
    before_b = _count_tenant_rows(substrate.dsn, "consent_records", tenant_b)
    assert before_a >= 1 and before_b >= 1, "fixture broken: no consent row seeded"

    purge_tenant_data(ticket_a)

    assert _count_tenant_rows(substrate.dsn, "consent_records", tenant_a) == before_a, (
        "consent_records must SURVIVE the purge (retention)"
    )
    assert _count_tenant_rows(substrate.dsn, "consent_records", tenant_b) == before_b, (
        "co-resident tenant's consent_records must be untouched"
    )


def test_purge_preserves_privacy_audit_log_dpdp_retention(substrate):  # type: ignore[no-untyped-def]
    """privacy_audit_log entries for the purged tenant are NOT deleted
    (DPDP 7-year retention). One ``subject_data_purged`` intent event
    plus one ``subject_data_purged_table`` row per table in
    ``_PURGE_ORDER`` are appended (VT-185 Q1 Option A — Cowork
    plan-review 2026-05-26 locked the 1-intent + N-table audit
    contract for CL-390 full-granularity compliance)."""
    from orchestrator.dsr_purge import _PURGE_ORDER, purge_tenant_data

    tenant_id = _new_tenant(substrate.dsn, name="Audit retention test")
    _seed_full_tenant_data(substrate.dsn, tenant_id)
    ticket_id = _open_dsr_ticket(substrate.dsn, tenant_id)

    audit_count_before = _count_tenant_rows(
        substrate.dsn, "privacy_audit_log", tenant_id
    )
    assert audit_count_before >= 1, "fixture broken: no audit row seeded"

    purge_tenant_data(ticket_id)

    audit_count_after = _count_tenant_rows(
        substrate.dsn, "privacy_audit_log", tenant_id
    )
    # Pre-existing row survives (DPDP retention) + purge writer
    # appended 1 intent + N per-table rows (VT-185).
    expected_added = 1 + len(_PURGE_ORDER)
    assert audit_count_after == audit_count_before + expected_added, (
        f"privacy_audit_log count: before={audit_count_before} "
        f"after={audit_count_after} — expected +{expected_added} "
        f"(1 intent + {len(_PURGE_ORDER)} per-table audit rows)"
    )

    # Confirm 1 intent + N per-table audit rows.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        intent_row = conn.execute(
            "SELECT count(*) FROM privacy_audit_log "
            "WHERE tenant_id = %s AND event_type = 'subject_data_purged'",
            (str(tenant_id),),
        ).fetchone()
        per_table_row = conn.execute(
            "SELECT count(*) FROM privacy_audit_log "
            "WHERE tenant_id = %s "
            "  AND event_type = 'subject_data_purged_table'",
            (str(tenant_id),),
        ).fetchone()
    assert intent_row is not None
    assert int(intent_row[0]) == 1
    assert per_table_row is not None
    assert int(per_table_row[0]) == len(_PURGE_ORDER)


def test_purge_is_idempotent_on_completed_ticket(substrate):  # type: ignore[no-untyped-def]
    """A second purge call on the same ticket is a no-op. No DELETEs
    fire on the replay; ``already_completed`` is True."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_id = _new_tenant(substrate.dsn, name="Idempotency test")
    _seed_full_tenant_data(substrate.dsn, tenant_id)
    ticket_id = _open_dsr_ticket(substrate.dsn, tenant_id)

    first = purge_tenant_data(ticket_id)
    assert first.already_completed is False

    second = purge_tenant_data(ticket_id)
    assert second.already_completed is True
    assert second.deleted_counts == {}
    assert second.tenant_anonymized is False

    # tenants row still anonymized (first call's state preserved).
    tenant_row = _tenant_row(substrate.dsn, tenant_id)
    assert tenant_row is not None
    assert tenant_row["business_name"] == "[deleted]"

    # Ticket still completed.
    ticket_row = _ticket_row(substrate.dsn, ticket_id)
    assert ticket_row["status"] == "completed"


def test_dbos_layer_not_synchronously_purged_documented_finding(substrate):  # type: ignore[no-untyped-def]
    """DBOS workflow_status / operation_outputs are NOT tenant-indexed.
    Per-tenant deletion is not expressible via the framework helper.
    PR #47's time-based purge handles DBOS subject data on the ~2h
    cadence. Documents that the synchronous DSR purge does NOT touch
    the DBOS layer — privacy notice must say so.

    Concretely: ``orchestrator.dsr_purge`` does not import from
    ``orchestrator.dbos_purge``. A future change that adds the import
    would change the contract; lock that against silent breakage."""
    import inspect
    import re

    from orchestrator import dsr_purge

    source = inspect.getsource(dsr_purge)
    # Match actual imports / function calls, not docstring prose. The
    # docstring intentionally references ``dbos_purge`` to explain the
    # design choice; a naked substring match would false-positive on
    # that. Look for import statements + qualified-name calls.
    import_pattern = re.compile(
        r"^\s*(from\s+orchestrator\.dbos_purge|import\s+orchestrator\.dbos_purge)",
        re.MULTILINE,
    )
    assert import_pattern.search(source) is None, (
        "dsr_purge MUST NOT import dbos_purge — the DBOS layer is "
        "time-based per PR #47, not tenant-scoped. If this changes, "
        "the privacy notice's RETAINED section must be updated in "
        "lockstep."
    )

    # Raw SQL against dbos.workflow_status would be a clear violation —
    # match the SQL-token pattern only inside strings, not comments /
    # docstrings.
    sql_pattern = re.compile(
        r"(?:UPDATE|DELETE\s+FROM|INSERT\s+INTO|FROM)\s+dbos\.workflow_status",
        re.IGNORECASE,
    )
    assert sql_pattern.search(source) is None, (
        "dsr_purge MUST NOT issue raw SQL against dbos.workflow_status "
        "— framework-managed, see PR #47 / VT-150-fix-1."
    )

    # Silence unused-import — datetime referenced by other tests.
    _ = datetime.now(UTC)


# --- VT-OIV: focused owner_inputs DSR-purge coverage ------------------------


def test_owner_inputs_dsr_purge_covers_substrate(substrate):  # type: ignore[no-untyped-def]
    """Brief goal-item 5: DSR purge deletes ALL owner_inputs rows for
    the purged tenant (regardless of ``consumed_at`` state) and does
    NOT touch a second tenant's owner_inputs row.

    The broad ``test_purge_clears_subject_data_across_all_inventoried_tables``
    above sweeps every purgeable table including owner_inputs; this
    focused row pins (a) the per-feature delete count and (b) the
    cross-tenant non-leak with mixed ``consumed_at`` state, so a
    future regression scoping the purge to ``consumed_at IS NULL``
    rows only would fail loud here.
    """
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_alpha = _new_tenant(substrate.dsn, name="tenant_alpha")
    tenant_bravo = _new_tenant(substrate.dsn, name="tenant_bravo")

    # Seed tenant_alpha: 1 pipeline_run + 3 owner_inputs rows with
    # mixed consumed_at (1 pending, 2 consumed) — proves the purge
    # does not depend on the pending filter.
    alpha_run = uuid4()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (str(alpha_run), str(tenant_alpha)),
        )
        conn.execute(
            "INSERT INTO owner_inputs (tenant_id, run_id, message_sid, "
            "intent, segment, occasion, consumed_at) VALUES "
            "(%s, %s, %s, 'winback', 'dormant_60d', 'diwali', NULL),"
            "(%s, %s, %s, 'campaign_request', 'vip', 'newyear', now()),"
            "(%s, %s, %s, 'feedback', NULL, NULL, now())",
            (
                str(tenant_alpha), str(alpha_run), f"SM{uuid4().hex}",
                str(tenant_alpha), str(alpha_run), f"SM{uuid4().hex}",
                str(tenant_alpha), str(alpha_run), f"SM{uuid4().hex}",
            ),
        )

    # Seed tenant_bravo: 1 pipeline_run + 1 owner_inputs row.
    bravo_run = uuid4()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (str(bravo_run), str(tenant_bravo)),
        )
        conn.execute(
            "INSERT INTO owner_inputs (tenant_id, run_id, message_sid, "
            "intent, segment, occasion) "
            "VALUES (%s, %s, %s, 'winback', 'dormant_60d', 'diwali')",
            (str(tenant_bravo), str(bravo_run), f"SM{uuid4().hex}"),
        )

    assert _count_tenant_rows(substrate.dsn, "owner_inputs", tenant_alpha) == 3
    assert _count_tenant_rows(substrate.dsn, "owner_inputs", tenant_bravo) == 1

    ticket_alpha = _open_dsr_ticket(substrate.dsn, tenant_alpha)
    result = purge_tenant_data(ticket_alpha)

    assert result.tenant_id == tenant_alpha
    assert result.deleted_counts.get("owner_inputs") == 3, (
        f"expected owner_inputs delete count = 3 for tenant_alpha; "
        f"got {result.deleted_counts}"
    )
    assert _count_tenant_rows(substrate.dsn, "owner_inputs", tenant_alpha) == 0
    # tenant_bravo's row is untouched.
    assert _count_tenant_rows(substrate.dsn, "owner_inputs", tenant_bravo) == 1


# --- VT-154: unscoped-DELETE guard (source-level, no DB) ---------------------


def test_vt154_unscoped_delete_guard_tenant_predicate_present():
    """VT-154 — fail-loud guard on the DSR purge privileged path.

    ``purge_tenant_data`` runs on the BYPASSRLS service-role pool (see
    dsr_purge module docstring), so the ``WHERE tenant_id = %s`` predicate is
    the SOLE scoping surface — a future edit that silently drops it would
    cross-tenant DELETE on a service-role connection. This source-level test
    fails CI if (1) ``_delete_where_tenant`` loses its parametrized tenant
    predicate, or (2) any ``DELETE FROM`` appears in the module without a
    tenant_id predicate in the same statement. Mirrors the inspect.getsource
    guard pattern of test_dbos_layer_not_synchronously_purged_documented_finding.
    """
    import inspect
    import re

    from orchestrator import dsr_purge

    # (1) _delete_where_tenant MUST keep the parametrized tenant predicate.
    delete_src = inspect.getsource(dsr_purge._delete_where_tenant)
    assert re.search(
        r"DELETE\s+FROM\s+\{table\}\s+WHERE\s+tenant_id\s*=\s*%s", delete_src
    ), (
        "VT-154: _delete_where_tenant lost its `WHERE tenant_id = %s` predicate — "
        "the SOLE scoping surface on the BYPASSRLS purge path. Restore it or DSR "
        "purge cross-tenant-deletes on the service-role connection."
    )

    # (2) No UNSCOPED DELETE anywhere in the module: EVERY `DELETE FROM <x>`
    #     statement must carry a tenant_id predicate (no escape hatch — the one
    #     legitimate DELETE is `DELETE FROM {table} WHERE tenant_id = %s`, which
    #     contains the token; a new `DELETE FROM {table}` without WHERE is caught).
    module_src = inspect.getsource(dsr_purge)
    for match in re.finditer(r"DELETE\s+FROM\s+[^\n]*", module_src, re.IGNORECASE):
        stmt = match.group(0).lower()
        assert "tenant_id" in stmt, (
            f"VT-154: unscoped DELETE found in dsr_purge: {match.group(0)!r}. "
            "Every DELETE on the privileged purge path MUST be tenant-scoped."
        )


# --- VT-160: tenant-anonymize completeness ----------------------------------


def test_vt160_anonymize_scrubs_all_identifying_columns(substrate):  # type: ignore[no-untyped-def]
    """VT-160 — DSR purge must irreversibly scrub EVERY identifying column on
    the tenants row, not just business_name/whatsapp_number.

    Pre-VT-160 the anonymize left owner_phone (mig 050, globally-UNIQUE-indexed
    → the strongest re-id anchor), owner_contact (mig 066) and locality (mig 001)
    intact — the subject stayed re-identifiable after a deletion DSR (DPDP-
    incomplete). Asserts: post-anonymize NO original PII value survives in ANY
    identifying column, the scrub is NULL (not a predictable/reversible token),
    the tenants row is KEPT (FK integrity for privacy_audit_log + dsr_tickets),
    and the scrub is idempotent on replay.
    """
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_id = _new_tenant(substrate.dsn, name="VT-160 subject")
    before = _tenant_row(substrate.dsn, tenant_id)
    assert before is not None
    # Sanity: the seed actually planted PII in every identifying column.
    seeded_pii = {
        before["business_name"], before["whatsapp_number"],
        before["owner_phone"], before["owner_contact"], before["locality"],
    }
    for col in ("whatsapp_number", "owner_phone", "owner_contact", "locality"):
        assert before[col], f"seed planted no PII in {col}"

    ticket_id = _open_dsr_ticket(substrate.dsn, tenant_id)
    result = purge_tenant_data(ticket_id)
    assert result.tenant_anonymized is True

    after = _tenant_row(substrate.dsn, tenant_id)
    assert after is not None, "tenants row must be KEPT (FK integrity), not deleted"
    # business_name is tombstoned; every other identifying anchor is NULL.
    assert after["business_name"] == "[deleted]"
    assert after["whatsapp_number"] is None
    assert after["owner_phone"] is None
    assert after["owner_contact"] is None
    assert after["locality"] is None
    # No original PII survives anywhere on the row (no reversible token).
    surviving = {v for v in after.values() if isinstance(v, str)}
    leaked = (seeded_pii & surviving) - {"[deleted]"}
    assert not leaked, f"VT-160: original PII survived the anonymize: {leaked}"

    # Idempotent replay: the ticket is already completed → no-op, row stays scrubbed.
    replay = purge_tenant_data(ticket_id)
    assert replay.already_completed is True
    after2 = _tenant_row(substrate.dsn, tenant_id)
    assert after2["owner_phone"] is None
    assert after2["owner_contact"] is None
    assert after2["locality"] is None


def test_vt160_anonymize_set_covers_every_anonymize_constant():
    """VT-160 guard: ``_anonymize_tenant_row`` builds its UPDATE from
    ``_TENANT_ANONYMIZE`` (dict-driven), so adding a new identifying column =
    one dict entry with no dict/UPDATE drift. Asserts the SET clause is derived
    from the dict keys (not a hand-listed column set that can silently fall out
    of sync). Source-level — no DB."""
    import inspect

    from orchestrator import dsr_purge

    src = inspect.getsource(dsr_purge._anonymize_tenant_row)
    assert "_TENANT_ANONYMIZE" in src, (
        "VT-160: _anonymize_tenant_row must derive its SET clause from "
        "_TENANT_ANONYMIZE so new identifying columns can't drift out of the "
        "UPDATE. Hand-listing columns reintroduces the gap VT-160 closed."
    )
    # The 3 columns VT-160 added must be present in the constant set.
    for col in ("owner_phone", "owner_contact", "locality"):
        assert col in dsr_purge._TENANT_ANONYMIZE, (
            f"VT-160: {col} (identifying PII) missing from _TENANT_ANONYMIZE"
        )
        assert dsr_purge._TENANT_ANONYMIZE[col] is None, (
            f"VT-160: {col} must scrub to NULL (irreversible), not a token"
        )


def test_purge_hard_deletes_episodic_events_l2(substrate):  # type: ignore[no-untyped-def]
    """VT-323 — the explicit L2 privacy gate. A tenant DSR-delete HARD-deletes
    its episodic_events (PII can sit in payload at rest), and a co-resident
    tenant's L2 rows are untouched. Real PG (mock cursors hide the FK/scoping)."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_a = _new_tenant(substrate.dsn, name="Tenant A (purgee)")
    tenant_b = _new_tenant(substrate.dsn, name="Tenant B (untouched)")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    _seed_full_tenant_data(substrate.dsn, tenant_b)

    # Pre: both have L2 rows.
    assert _count_tenant_rows(substrate.dsn, "episodic_events", tenant_a) >= 1
    assert _count_tenant_rows(substrate.dsn, "episodic_events", tenant_b) >= 1

    result = purge_tenant_data(_open_dsr_ticket(substrate.dsn, tenant_a))
    assert result.deleted_counts.get("episodic_events", 0) >= 1, (
        "VT-323: purge must report episodic_events deletions"
    )

    # Post: A's L2 store is GONE; B's survives (scoping).
    assert _count_tenant_rows(substrate.dsn, "episodic_events", tenant_a) == 0, (
        "VT-323: tenant DSR-delete left L2 episodic rows behind (PII survives)"
    )
    assert _count_tenant_rows(substrate.dsn, "episodic_events", tenant_b) >= 1, (
        "VT-323: cross-tenant leak — purging A wiped B's L2 rows"
    )


def test_purge_hard_deletes_tenant_oauth_tokens(substrate):  # type: ignore[no-untyped-def]
    """VT-422 GAP-1 — the DPDP erasure bug. The per-tenant ENCRYPTED OAuth credential
    (Shopify offline token) was EXPORTED on DSR but never ERASED: it FKs tenants, but the
    tenant row is anonymized (not deleted) so the CASCADE never fires, and the table was
    missing from _PURGE_ORDER → the credential survived erasure. A tenant DSR-delete must
    HARD-DELETE the token row (assert 0 rows after purge); a co-resident tenant is untouched.
    Real PG (mock cursors hide the FK/scoping)."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_a = _new_tenant(substrate.dsn, name="Tenant A (oauth purgee)")
    tenant_b = _new_tenant(substrate.dsn, name="Tenant B (oauth untouched)")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    _seed_full_tenant_data(substrate.dsn, tenant_b)

    # Pre: both have a stored encrypted OAuth credential.
    assert _count_tenant_rows(substrate.dsn, "tenant_oauth_tokens", tenant_a) >= 1
    assert _count_tenant_rows(substrate.dsn, "tenant_oauth_tokens", tenant_b) >= 1

    result = purge_tenant_data(_open_dsr_ticket(substrate.dsn, tenant_a))
    assert result.deleted_counts.get("tenant_oauth_tokens", 0) >= 1, (
        "VT-422 GAP-1: purge must report tenant_oauth_tokens deletions"
    )

    # Post: A's encrypted credential is GONE; B's survives (scoping). This is THE
    # privacy-at-rest assertion — the credential must not outlive erasure.
    assert _count_tenant_rows(substrate.dsn, "tenant_oauth_tokens", tenant_a) == 0, (
        "VT-422 GAP-1: DSR-delete left the encrypted OAuth token behind (credential survives erasure)"
    )
    assert _count_tenant_rows(substrate.dsn, "tenant_oauth_tokens", tenant_b) >= 1, (
        "VT-422 GAP-1: cross-tenant leak — purging A wiped B's OAuth token"
    )


def test_purge_hard_deletes_tm_audit_and_debug_events(substrate):  # type: ignore[no-untyped-def]
    """VT-518 (DSR-purge gap, Cowork audit-after of VT-514/515) — the two tenant-scoped
    PII-bearing observability tables added by VT-514 (tm_audit_log) + VT-515 (debug_events)
    were missing from _PURGE_ORDER, so a right-to-erasure tenant's audit + debug activity
    history survived the purge. Redact-at-write + RLS is insufficient for ERASURE — the
    redacted history is still the subject's data. A tenant DSR-delete MUST hard-delete both
    (assert 0 rows after purge); a co-resident tenant is untouched. Real PG — mock cursors
    hide the FK-ordering (tm_audit_log.run_id → pipeline_runs; parent_audit_id self-FK)."""
    from orchestrator.dsr_purge import _PURGE_ORDER, purge_tenant_data

    # Both tables are in the purge order (drift guard — a future edit dropping either
    # silently re-opens the erasure gap).
    assert "tm_audit_log" in _PURGE_ORDER, "tm_audit_log fell out of _PURGE_ORDER (DSR gap)"
    assert "debug_events" in _PURGE_ORDER, "debug_events fell out of _PURGE_ORDER (DSR gap)"

    tenant_a = _new_tenant(substrate.dsn, name="Tenant A (tm-audit purgee)")
    tenant_b = _new_tenant(substrate.dsn, name="Tenant B (tm-audit untouched)")
    _seed_full_tenant_data(substrate.dsn, tenant_a)
    _seed_full_tenant_data(substrate.dsn, tenant_b)

    # Pre: both tenants have audit + debug rows.
    for table in ("tm_audit_log", "debug_events"):
        assert _count_tenant_rows(substrate.dsn, table, tenant_a) >= 1
        assert _count_tenant_rows(substrate.dsn, table, tenant_b) >= 1

    result = purge_tenant_data(_open_dsr_ticket(substrate.dsn, tenant_a))
    for table in ("tm_audit_log", "debug_events"):
        assert result.deleted_counts.get(table, 0) >= 1, (
            f"VT-518: purge must report {table} deletions"
        )
        # Post: A's history is GONE; B's survives (scoping). THE erasure assertion.
        assert _count_tenant_rows(substrate.dsn, table, tenant_a) == 0, (
            f"VT-518: DSR-delete left {table} rows behind (subject activity survives erasure)"
        )
        assert _count_tenant_rows(substrate.dsn, table, tenant_b) >= 1, (
            f"VT-518: cross-tenant leak — purging A wiped B's {table}"
        )
