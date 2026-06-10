"""VT-366 Gap-2a — DB-backed behavioral tests for the Auto-Discovery DRAFT
business profile + the owner-confirm promotion gate
(``orchestrator.onboarding.draft_profile``) and its DSR hard-delete canary.

The load-bearing invariant (CL-390): a DRAFT assembled from public sources is
NEVER asserted to the canonical ``business_profile`` (l1_entities) or the KG
until the owner CONFIRMS it. ``confirm_draft`` is the single promotion gate and
promotes ONLY the owner-confirmed (possibly owner-edited) fields — unconfirmed
draft fields stay drafts forever.

Second invariant: the draft table is tenant-scoped + RLS'd (migration 122) AND
swept by ``dsr_purge`` — a new tenant table forgotten in ``_PURGE_ORDER`` is the
recurring DSR drift. The hard-delete canary proves it.

Requires a real Postgres + the dbos stack. Mirrors the patterns in
``tests/orchestrator/test_dsr_purge_substrate.py``: migrations applied once,
DBOS launched so the substrate pool exists, tenants/tickets seeded via a direct
service-role (BYPASSRLS) psycopg connection. ``write_draft`` / ``get_draft`` /
``confirm_draft`` go through ``tenant_connection`` (the RLS'd app_role path), so
they need the launched substrate; the assertions read back via direct
service-role SELECTs (which see all tenants).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-366 draft_profile substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``graph._pool`` (the substrate the
    tenant_connection path resolves) exists. Mirrors test_dsr_purge_substrate."""
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


# --- Seeding + readback helpers (direct service-role / BYPASSRLS) ----------


def _new_tenant(dsn: str, *, name: str = "VT-366 draft test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "whatsapp_number, owner_phone) "
            "VALUES (%s, 'founding', 'paid_active', %s, %s) RETURNING id",
            (
                name,
                f"+9199{uuid4().int % 10**8:08d}",
                f"+9188{uuid4().int % 10**8:08d}",
            ),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _open_dsr_ticket(dsn: str, tenant_id: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, "
            "acknowledged_at) VALUES (%s, 'deletion', 'acknowledged', now()) "
            "RETURNING id",
            (str(tenant_id),),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _count_tenant_rows(dsn: str, table: str, tenant_id: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s",  # noqa: S608 — fixed table name
            (str(tenant_id),),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _canonical_profile_attributes(dsn: str, tenant_id: UUID) -> dict | None:
    """The canonical business_profile attributes via a direct service-role SELECT
    on l1_entities (entity_type='business_profile'). ``None`` if no row exists —
    i.e. nothing was ever promoted. Default psycopg row_factory → tuple."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities "
            "WHERE tenant_id = %s AND entity_type = 'business_profile'",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return dict(row[0] or {})


# --- Test 1: the promotion gate --------------------------------------------


def test_confirm_promotes_only_confirmed_fields(substrate):  # type: ignore[no-untyped-def]
    """THE accuracy/privacy boundary (CL-390): ``confirm_draft`` promotes ONLY
    the owner-confirmed (and possibly owner-EDITED) fields to the canonical
    business_profile. Unconfirmed draft fields are NEVER asserted.

    Draft holds {business_name, category, rating, website} from public sources.
    The owner confirms a SUBSET — an EDITED business_name + a NEW city
    ("Bengaluru") they typed — and drops everything else. The canonical profile
    must contain ONLY {business_name=edited, city} and NONE of the un-confirmed
    draft fields (category, rating, website).
    """
    from orchestrator.onboarding.draft_profile import write_draft

    tenant = _new_tenant(substrate.dsn, name="confirm-gate tenant")

    # Auto-Discovery writes a draft: GBP fields + a website-sourced field.
    write_draft(
        tenant,
        {
            "business_name": "Auto-Guessed Cafe Pvt Ltd",  # public-source guess
            "category": "restaurant",
            "rating": 4.3,
        },
        source="gbp",
    )
    write_draft(
        tenant,
        {"website": "https://example-discovered.in"},
        source="website",
    )

    # Sanity: the draft row exists and carries all four drafted fields, but
    # NOTHING has been promoted to the canonical profile yet.
    from orchestrator.onboarding.draft_profile import get_draft

    draft = get_draft(tenant)
    assert set(draft["attributes"]) == {
        "business_name",
        "category",
        "rating",
        "website",
    }, draft["attributes"]
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None, (
        "draft must NOT touch the canonical business_profile before confirm"
    )

    # The owner confirms a SUBSET, with an EDIT to business_name + a NEW field
    # (city) they typed in the confirm UI. emit_kg=False to decouple from the
    # KG outbox/drain (downstream of the authoritative L1 promotion under test).
    from orchestrator.onboarding.draft_profile import confirm_draft

    edited_name = "Owner's Real Cafe"
    confirm_draft(
        tenant,
        {"business_name": edited_name, "city": "Bengaluru"},
        emit_kg=False,
    )

    promoted = _canonical_profile_attributes(substrate.dsn, tenant)
    assert promoted is not None, "confirm_draft did not create the canonical profile"

    # ONLY the confirmed fields are present, with the owner's edit applied.
    assert promoted.get("business_name") == edited_name, (
        f"confirmed business_name must be the owner-edited value; got "
        f"{promoted.get('business_name')!r}"
    )
    assert promoted.get("city") == "Bengaluru"

    # The un-confirmed draft fields were NEVER asserted (the whole point).
    for unconfirmed in ("category", "rating", "website"):
        assert unconfirmed not in promoted, (
            f"un-confirmed draft field {unconfirmed!r} leaked into the canonical "
            f"business_profile — confirm gate breached. profile={promoted!r}"
        )


# --- Test 2: the DSR hard-delete canary ------------------------------------


def test_business_profile_draft_dsr_hard_deleted(substrate):  # type: ignore[no-untyped-def]
    """VT-366 DSR canary (Cowork-required): a tenant DSR-delete HARD-deletes its
    business_profile_draft row, and the table is in ``_PURGE_ORDER`` (the cheap
    coverage check that catches a future table forgotten in the purge order)."""
    from orchestrator.dsr_purge import _PURGE_ORDER, purge_tenant_data
    from orchestrator.onboarding.draft_profile import write_draft

    # Cheap coverage check: the table is wired into the purge order at all.
    assert "business_profile_draft" in _PURGE_ORDER, (
        "business_profile_draft missing from dsr_purge._PURGE_ORDER — a tenant "
        "table forgotten in the purge order is the recurring DSR drift (CL-390)"
    )

    tenant = _new_tenant(substrate.dsn, name="dsr draft canary")
    write_draft(
        tenant,
        {"business_name": "Draft To Be Purged", "category": "cafe"},
        source="gbp",
    )

    # Pre: the draft row exists for this tenant.
    assert _count_tenant_rows(substrate.dsn, "business_profile_draft", tenant) == 1, (
        "fixture broken: write_draft did not create a business_profile_draft row"
    )

    ticket = _open_dsr_ticket(substrate.dsn, tenant)
    result = purge_tenant_data(ticket)

    assert result.tenant_id == tenant
    assert result.deleted_counts.get("business_profile_draft") == 1, (
        f"purge must report exactly 1 business_profile_draft deletion; got "
        f"{result.deleted_counts.get('business_profile_draft')!r}"
    )

    # Post: the draft row is GONE (hard delete, not anonymized).
    assert _count_tenant_rows(substrate.dsn, "business_profile_draft", tenant) == 0, (
        "VT-366: DSR purge left a business_profile_draft row behind — public-"
        "source PII survives a deletion DSR"
    )
