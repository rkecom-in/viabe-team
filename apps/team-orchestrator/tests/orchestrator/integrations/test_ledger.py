"""VT-273 — customer ledger write path tests.

PURE: entry_key determinism, enum reject, input validation.
DB: write / idempotent re-ingest / low-confidence deferred / cross-tenant FK /
resolve_customer_by_phone_token (N2 — proves dedup_and_merge populates
phone_token_resolutions.customer_id). Real Postgres, no mock cursors.
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.dedup_merge import AcquiredViaError  # noqa: E402
from orchestrator.integrations.ledger import (  # noqa: E402
    LedgerEntryIn,
    _entry_key,
    record_ledger_entries,
)

_T = "11111111-1111-4111-8111-111111111111"
_C = "22222222-2222-4222-8222-222222222222"


def _entry(conf=0.9, amount=150000, etype="sale"):
    return LedgerEntryIn(amount_paise=amount, entry_type=etype,
                         entry_date=date(2026, 6, 1), confidence=conf)


# --- PURE ---------------------------------------------------------------------

def test_entry_key_deterministic_and_distinct():
    a = _entry_key(_T, _C, _entry())
    assert a == _entry_key(_T, _C, _entry())               # same inputs → same key
    assert a != _entry_key(_T, _C, _entry(amount=160000))  # amount differs → differs
    assert a != _entry_key(_T, _C, _entry(etype="payment"))


def test_invalid_acquired_via_rejected_before_db():
    with pytest.raises(AcquiredViaError):
        record_ledger_entries(_T, _C, [_entry()], acquired_via="not_a_method")


def test_entry_validation_bounds():
    with pytest.raises(Exception):
        LedgerEntryIn(amount_paise=-1, entry_type="sale",
                      entry_date=date(2026, 6, 1), confidence=0.9)
    with pytest.raises(Exception):
        LedgerEntryIn(amount_paise=1, entry_type="sale",
                      entry_date=date(2026, 6, 1), confidence=1.5)


# --- DB -----------------------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — ledger DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    # dedup_and_merge registers an encrypted phone_token on insert (VT-191).
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
            "VALUES ('VT-273 ledger test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _customer(tenant: str, phone: str) -> str:
    from orchestrator.integrations.dedup_merge import dedup_and_merge

    r = dedup_and_merge(tenant, acquired_via="paper_book", phone_e164=phone)
    return str(r.customer_id)


def _uniq_phone() -> str:
    return "+9190" + uuid4().int.__str__()[:8]


@_DB
def test_record_and_idempotent_reingest(db_ctx):
    from orchestrator.db import tenant_connection

    tenant = _tenant(db_ctx.dsn)
    cust = _customer(tenant, _uniq_phone())
    entries = [_entry(0.9, 150000, "sale"), _entry(0.88, 5000, "payment")]

    r1 = record_ledger_entries(tenant, cust, entries, acquired_via="paper_book")
    assert (r1.written, r1.skipped_duplicate, r1.deferred_low_confidence) == (2, 0, 0)

    # Re-ingest the SAME ledger → idempotent (no new rows).
    r2 = record_ledger_entries(tenant, cust, entries, acquired_via="paper_book")
    assert (r2.written, r2.skipped_duplicate) == (0, 2)

    with tenant_connection(tenant) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries WHERE customer_id = %s",
            (cust,)).fetchone()["n"]
    assert n == 2, "re-ingest duplicated ledger rows"


@_DB
def test_low_confidence_deferred_not_written(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    cust = _customer(tenant, _uniq_phone())
    r = record_ledger_entries(tenant, cust, [_entry(conf=0.5)], acquired_via="paper_book")
    assert (r.written, r.deferred_low_confidence) == (0, 1)


@_DB
def test_cross_tenant_fk_blocks_foreign_customer(db_ctx):
    tenant_a = _tenant(db_ctx.dsn)
    tenant_b = _tenant(db_ctx.dsn)
    cust_a = _customer(tenant_a, _uniq_phone())
    # B tries to write a ledger entry against A's customer → composite FK
    # (tenant_b, cust_a) has no matching customers row → rejected.
    with pytest.raises(psycopg.Error):
        record_ledger_entries(tenant_b, cust_a, [_entry()], acquired_via="paper_book")


@_DB
def test_resolve_customer_by_phone_token(db_ctx):
    # N2: dedup_and_merge must populate phone_token_resolutions.customer_id so the
    # token resolver (VT-258's read seam) returns the right customer.
    from orchestrator.integrations.dedup_merge import dedup_and_merge
    from orchestrator.integrations.ledger import resolve_customer_by_phone_token
    from orchestrator.observability.phone_tokens import _hash_phone

    tenant_a = _tenant(db_ctx.dsn)
    tenant_b = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    r = dedup_and_merge(tenant_a, acquired_via="paper_book", phone_e164=phone)
    token = _hash_phone(phone)

    assert resolve_customer_by_phone_token(tenant_a, token) == r.customer_id
    # Cross-tenant: B cannot resolve A's token (RLS) → None.
    assert resolve_customer_by_phone_token(tenant_b, token) is None
