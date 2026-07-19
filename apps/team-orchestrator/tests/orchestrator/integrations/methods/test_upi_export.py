"""VT-57 — UPI export (Method 3) tests.

PURE: per-provider CSV parsing, paise-precise amounts, multi-format dates, VPA
extraction, direction, incomplete-row skip. DB (real Postgres, no mock cursors):
phone@upi → customer + ledger payment + VPA link recorded; exact VPA re-resolution
+ idempotent re-upload; unattributed credit → imported only; refund (debit to a
KNOWN customer) retained raw (D2/N1); unknown debit (owner→vendor) dropped;
cross-tenant isolation; PII absence in logs. Synthetic data only (CL-422).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.methods.upi_export import (  # noqa: E402
    _parse_amount_paise,
    _parse_date,
    _parse_upi_csv,
    _phone_from_vpa,
)

_NOW = datetime(2026, 6, 1)


# --- PURE: parsing ------------------------------------------------------------

def test_amount_paise_precision():
    assert _parse_amount_paise("1,500.50") == 150050
    assert _parse_amount_paise("1500") == 150000
    assert _parse_amount_paise("₹2,000") == 200000
    assert _parse_amount_paise("nope") is None


def test_date_multiple_formats():
    assert _parse_date("2026-06-01", _NOW) == date(2026, 6, 1)
    assert _parse_date("01/06/2026", _NOW) == date(2026, 6, 1)
    assert _parse_date("01-Jun-2026", _NOW) == date(2026, 6, 1)
    assert _parse_date("garbage", _NOW) is None


def test_phone_from_vpa():
    assert _phone_from_vpa("9876543210@oksbi") == "9876543210"
    assert _phone_from_vpa("asha.shop@okhdfc") is None
    assert _phone_from_vpa(None) is None


def test_phonepe_csv_credit_with_vpa_in_description():
    csv = ("Date,Type,Amount,Transaction ID,Transaction Details\n"
           "2026-06-01,CREDIT,1500.50,TXN-1,Received from 9876543210@oksbi\n")
    rows = _parse_upi_csv(csv, "phonepe", _NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r.direction == "credit" and r.amount_paise == 150050
    assert r.transaction_ref == "TXN-1" and r.payer_vpa == "9876543210@oksbi"


def test_gpay_csv_explicit_vpa_and_debit():
    csv = ("Date,Transaction Type,Amount (INR),UTR No.,UPI ID,Payer Name\n"
           "01/06/2026,Debit,300,UTR-9,asha@oksbi,Asha\n")
    rows = _parse_upi_csv(csv, "gpay", _NOW)
    assert len(rows) == 1 and rows[0].direction == "debit"
    assert rows[0].payer_vpa == "asha@oksbi" and rows[0].payer_name == "Asha"


def test_incomplete_row_skipped():
    # No transaction ref → cannot dedupe/idempotent → skipped (P4).
    csv = "Date,Type,Amount,Transaction ID\n2026-06-01,CREDIT,100,\n"
    assert _parse_upi_csv(csv, "paytm", _NOW) == []


# --- DB (real Postgres) -------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — upi_export DB tests skipped",
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
            "VALUES ('VT-57 upi test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _phone10() -> str:
    return "90" + uuid4().int.__str__()[:8]


def _counts(tenant: str) -> tuple[int, int, int]:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        imp = conn.execute(
            "SELECT count(*) AS n FROM imported_transactions").fetchone()["n"]
        led = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
        cust = conn.execute("SELECT count(*) AS n FROM customers").fetchone()["n"]
    return imp, led, cust


@_DB
def test_phone_vpa_credit_attributes_and_records_link(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.upi_export import ingest_upi_export

    tenant = _tenant(db_ctx.dsn)
    phone = _phone10()
    csv = (f"Date,Type,Amount,Transaction ID,Transaction Details\n"
           f"2026-06-01,CREDIT,1500,TXN-A1,Received from {phone}@oksbi\n").encode()
    s = ingest_upi_export(tenant, csv, "phonepe")
    assert s.committed == 1 and s.dropped == 0  # attributed credit → ledger
    imp, led, cust = _counts(tenant)
    assert imp == 1 and led == 1 and cust == 1
    with tenant_connection(tenant) as conn:
        link = conn.execute(
            "SELECT count(*) AS n FROM upi_vpa_resolutions WHERE vpa = %s",
            (f"{phone}@oksbi",)).fetchone()["n"]
        # VT-417 PR-3: the promoted credit MUST land entry_type='sale' (a UPI
        # credit is a sale), so the Sales-Recovery detector — which counts ONLY
        # entry_type='sale' — sees it. 'payment' made every UPI sale invisible to
        # win-back targeting.
        entry_type = conn.execute(
            "SELECT entry_type FROM customer_ledger_entries").fetchone()["entry_type"]
    assert link == 1  # VPA→customer link recorded for next time
    assert entry_type == "sale"  # feeds win-back detection (not 'payment')


@_DB
def test_reupload_idempotent_and_vpa_reused(db_ctx):
    from orchestrator.integrations.methods.upi_export import ingest_upi_export

    tenant = _tenant(db_ctx.dsn)
    phone = _phone10()
    csv = (f"Date,Type,Amount,Transaction ID,Transaction Details\n"
           f"2026-06-01,CREDIT,1500,TXN-B1,Received from {phone}@oksbi\n").encode()
    ingest_upi_export(tenant, csv, "phonepe")
    ingest_upi_export(tenant, csv, "phonepe")  # re-upload same export
    imp, led, cust = _counts(tenant)
    assert imp == 1 and led == 1 and cust == 1  # no dupes; VPA reused, not re-created


@_DB
def test_unattributed_credit_imported_only(db_ctx):
    from orchestrator.integrations.methods.upi_export import ingest_upi_export

    tenant = _tenant(db_ctx.dsn)
    # Non-phone VPA, no prior link → unattributed → imported only.
    csv = ("Date,Type,Amount,Transaction ID,Transaction Details\n"
           "2026-06-01,CREDIT,800,TXN-C1,Received from someshop@okhdfc\n").encode()
    s = ingest_upi_export(tenant, csv, "phonepe")
    assert s.committed == 0 and s.parked == 1
    imp, led, cust = _counts(tenant)
    assert imp == 1 and led == 0 and cust == 0


@_DB
def test_refund_to_known_customer_retained_raw(db_ctx):
    """D2/N1: a debit to a KNOWN customer (refund) → retained raw, not promoted."""
    from orchestrator.integrations.methods.upi_export import ingest_upi_export

    tenant = _tenant(db_ctx.dsn)
    phone = _phone10()
    # 1) credit establishes the customer + VPA link.
    credit = (f"Date,Type,Amount,Transaction ID,Transaction Details\n"
              f"2026-06-01,CREDIT,1500,TXN-D1,Received from {phone}@oksbi\n").encode()
    ingest_upi_export(tenant, credit, "phonepe")
    # 2) debit to the SAME vpa = a refund to that known customer → retained.
    refund = (f"Date,Type,Amount,Transaction ID,Transaction Details\n"
              f"2026-06-01,DEBIT,200,TXN-D2,Paid to {phone}@oksbi\n").encode()
    s = ingest_upi_export(tenant, refund, "phonepe")
    assert s.dropped == 0  # known customer → retained, not dropped
    imp, led, _ = _counts(tenant)
    assert imp == 2 and led == 1  # refund raw-only; ledger still just the 1 credit


@_DB
def test_unknown_debit_dropped(db_ctx):
    """A debit to an UNKNOWN counterparty = owner→vendor payment → dropped."""
    from orchestrator.integrations.methods.upi_export import ingest_upi_export

    tenant = _tenant(db_ctx.dsn)
    csv = ("Date,Type,Amount,Transaction ID,Transaction Details\n"
           "2026-06-01,DEBIT,5000,TXN-E1,Paid to vendor@okaxis\n").encode()
    s = ingest_upi_export(tenant, csv, "phonepe")
    assert s.dropped == 1
    imp, led, cust = _counts(tenant)
    assert imp == 0 and led == 0 and cust == 0


@_DB
def test_cross_tenant_vpa_isolation(db_ctx):
    from orchestrator.integrations.methods.upi_export import ingest_upi_export, resolve_vpa

    a, b = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    phone = _phone10()
    csv = (f"Date,Type,Amount,Transaction ID,Transaction Details\n"
           f"2026-06-01,CREDIT,1500,TXN-F1,Received from {phone}@oksbi\n").encode()
    ingest_upi_export(a, csv, "phonepe")
    assert resolve_vpa(b, f"{phone}@oksbi") is None  # B cannot see A's VPA link


@_DB
def test_pii_absence_in_logs(db_ctx, caplog):
    from orchestrator.integrations.methods.upi_export import ingest_upi_export

    tenant = _tenant(db_ctx.dsn)
    phone = _phone10()
    csv = (f"Date,Type,Amount,Transaction ID,Transaction Details\n"
           f"2026-06-01,CREDIT,1500,TXN-G1,Received from {phone}@oksbi\n").encode()
    with caplog.at_level(logging.INFO):
        ingest_upi_export(tenant, csv, "phonepe")
    assert phone not in caplog.text and "@oksbi" not in caplog.text  # counts only (CL-390)
