"""VT-8.5 — consent-capture machinery (Rule #15 canary, real Postgres).

DB-substrate tests against a real Postgres (requires ``DATABASE_URL`` + the
dbos stack; runs in the CI ``orchestrator`` job which provisions
``pgvector/pgvector:pg16``, and in the pre-push migrations/orchestrator job).
No mock cursors — every assertion is against a row the production code path
actually wrote through ``tenant_connection`` (SET ROLE app_role + RLS).

Invariants under test (the VT-85 plan's canary):
  - consent write + version capture (record_consent → active row).
  - re-consent updates version + is idempotent on (tenant, phone_token).
  - opt-out → re-consent flips has_consent false → true (Fix 1: opted_out_at
    cleared on re-consent).
  - has_consent fail-CLOSED when no record.
  - NO raw PII in the row (assert the token, never the raw E.164 number).
  - cross-tenant RLS isolation.
  - per-customer DSR purge removes the row.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-8.5 consent substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt85-consent-test-salt")
    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


# --- helpers ---------------------------------------------------------------


def _new_tenant(dsn: str, *, name: str = "consent test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "whatsapp_number) VALUES (%s, 'founding', 'paid_active', %s) "
            "RETURNING id",
            (name, f"+9199{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _synthetic_phone() -> str:
    """A synthetic E.164 number (CL-422: dev = synthetic only)."""
    return f"+9198{uuid4().int % 10**8:08d}"


def _read_row(dsn: str, tenant_id: UUID, phone_token: str) -> dict | None:
    """Read the consent row via superuser (RLS bypassed at assert time)."""
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(
            "SELECT * FROM record_of_consent "
            "WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant_id), phone_token),
        ).fetchone()


# --- tests -----------------------------------------------------------------


def test_record_consent_and_has_consent(substrate):
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()

    rec = consent.record_consent(
        tenant, phone, consent_text_version="qr_consent_v0_draft_en"
    )
    assert rec.active is True
    assert rec.consent_text_version == "qr_consent_v0_draft_en"
    assert rec.phone_token.startswith("phone_tok_")
    assert consent.has_consent(tenant, rec.phone_token) is True


def test_no_raw_pii_persisted(substrate):
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()
    rec = consent.record_consent(
        tenant, phone, consent_text_version="qr_consent_v0_draft_en",
        source="qr-page", locale="en",
    )

    row = _read_row(substrate, tenant, rec.phone_token)
    assert row is not None
    # The token is stored; the raw number is NOWHERE in the row (CL-390).
    assert row["phone_token"] == rec.phone_token
    assert row["phone_token"].startswith("phone_tok_")
    for value in row.values():
        assert phone not in str(value), f"raw phone leaked in column value {value!r}"


def test_reconsent_idempotent_updates_version(substrate):
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()

    consent.record_consent(tenant, phone, consent_text_version="qr_consent_v0_draft_en")
    rec2 = consent.record_consent(tenant, phone, consent_text_version="qr_consent_v1")

    assert rec2.consent_text_version == "qr_consent_v1"
    # idempotent on (tenant, phone_token): exactly one row.
    with psycopg.connect(substrate, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM record_of_consent "
            "WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant), rec2.phone_token),
        ).fetchone()[0]
    assert count == 1


def test_optout_then_reconsent_flips_has_consent(substrate):
    """Fix 1: opt-out blocks; re-consent clears opted_out_at and un-blocks."""
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()

    rec = consent.record_consent(tenant, phone, consent_text_version="qr_consent_v0_draft_en")
    token = rec.phone_token
    assert consent.has_consent(tenant, token) is True

    assert consent.opt_out(tenant, token) is True
    assert consent.has_consent(tenant, token) is False
    row = _read_row(substrate, tenant, token)
    assert row is not None and row["opted_out_at"] is not None

    # re-consent: opted_out_at reset to NULL, has_consent true again.
    consent.record_consent(tenant, phone, consent_text_version="qr_consent_v0_draft_en")
    assert consent.has_consent(tenant, token) is True
    row = _read_row(substrate, tenant, token)
    assert row is not None and row["opted_out_at"] is None

    # opt_out on an already-active->withdrawn row returns False the 2nd time.
    assert consent.opt_out(tenant, token) is True
    assert consent.opt_out(tenant, token) is False


def test_has_consent_fail_closed_when_no_record(substrate):
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    assert consent.has_consent(tenant, "phone_tok_does_not_exist") is False


def test_cross_tenant_rls_isolation(substrate):
    from orchestrator.privacy import consent

    tenant_a = _new_tenant(substrate, name="tenant A")
    tenant_b = _new_tenant(substrate, name="tenant B")
    phone = _synthetic_phone()

    rec = consent.record_consent(tenant_a, phone, consent_text_version="qr_consent_v0_draft_en")
    # tenant B cannot see tenant A's consent (RLS), even with the same token.
    assert consent.has_consent(tenant_a, rec.phone_token) is True
    assert consent.has_consent(tenant_b, rec.phone_token) is False


def test_purge_consent_removes_row(substrate):
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()
    rec = consent.record_consent(tenant, phone, consent_text_version="qr_consent_v0_draft_en")

    assert consent.purge_consent(tenant, rec.phone_token) == 1
    assert consent.has_consent(tenant, rec.phone_token) is False
    assert _read_row(substrate, tenant, rec.phone_token) is None
