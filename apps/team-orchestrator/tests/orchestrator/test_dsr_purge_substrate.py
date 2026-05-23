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
    apply_migrations.apply(dsn=dsn)
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
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "whatsapp_number) VALUES (%s, 'founding', 'paid_active', %s) "
            "RETURNING id",
            (name, f"+9199{uuid4().int % 10**8:08d}"),
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
            "(run_id, tenant_id, step_index, step_kind, input_envelope) "
            "VALUES (%s, %s, 0, 'webhook_received', '{}'::jsonb)",
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

        # phone_token_resolutions (PK is ``token``; schema columns:
        # token, tenant_id, phone_number_encrypted, resolved_count,
        # last_resolved_at, created_at)
        conn.execute(
            "INSERT INTO phone_token_resolutions (token, tenant_id, "
            "phone_number_encrypted, last_resolved_at) "
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

        # privacy_audit_log — pre-existing event, MUST survive purge
        conn.execute(
            "INSERT INTO privacy_audit_log (tenant_id, event_type, "
            "payload, this_hash, actor) "
            "VALUES (%s, 'pre_purge_event', '{}'::jsonb, %s, 'test')",
            (str(tenant_id), uuid4().hex),
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
            "SELECT business_name, whatsapp_number, opt_out FROM tenants "
            "WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "business_name": row[0],
        "whatsapp_number": row[1],
        "opt_out": row[2],
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
    "owner_inputs",
    "campaigns",
    "pipeline_steps",
    "pipeline_runs",
    "subscriber_states",
    "phase_transitions",
    "subscriptions",
    "phone_token_resolutions",
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


def test_purge_preserves_privacy_audit_log_dpdp_retention(substrate):  # type: ignore[no-untyped-def]
    """privacy_audit_log entries for the purged tenant are NOT deleted
    (DPDP 7-year retention). A new ``subject_data_purged`` event is
    appended."""
    from orchestrator.dsr_purge import purge_tenant_data

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
    # Pre-existing row survives + purge writer appended one
    # 'subject_data_purged' event.
    assert audit_count_after == audit_count_before + 1, (
        f"privacy_audit_log count: before={audit_count_before} "
        f"after={audit_count_after} — DPDP retention violated"
    )

    # Confirm the new event is the purge marker.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        purge_events_row = conn.execute(
            "SELECT count(*) FROM privacy_audit_log "
            "WHERE tenant_id = %s AND event_type = 'subject_data_purged'",
            (str(tenant_id),),
        ).fetchone()
    assert purge_events_row is not None
    assert int(purge_events_row[0]) == 1


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
