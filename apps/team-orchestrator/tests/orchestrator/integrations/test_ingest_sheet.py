"""VT-417 PR-2 — sheet/drive/integration_push/scheduler de-stub tests + the
synthetic Sheet canary (Rule #15: a real write end-to-end against a throwaway
PG16+pgvector).

PURE: the sheet-row → CanonicalRow mapper (column-alias resolution, amount→paise,
date parsing, identity-only vs sale rows, consent always None), and the
``GoogleSheetConnector.pull_full`` data-surfacing fix (header-zipped row dicts).
DB: ``ingest_customer_rows`` via the SHEET lineage lands a real customers row +
a real ``sale`` customer_ledger_entries row; a contacts-only sheet lands
identity-only; the sheet_push handler round-trips a synthetic HMAC-signed payload
into real rows (the canary); re-delivery is idempotent.

Real Postgres (DATABASE_URL), no mock cursors (the VT-263 / Cowork bar).
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.dedup_merge import ACQUIRED_VIA  # noqa: E402
from orchestrator.integrations.ingest import (  # noqa: E402
    _normalize_e164,
    sheet_row_to_canonical,
)


# --- PURE: enum tags (PR-2 lineage) -------------------------------------------

def test_sheet_enum_tags_present():
    # The writers RAISE on an unknown tag; the sheet lineage needs these two.
    assert {"google_sheet", "drive_sheet"} <= ACQUIRED_VIA


# --- PURE: column-alias resolution + identity ---------------------------------

def test_mapper_phone_aliases_normalize_e164():
    for col in ("phone", "Phone", "Mobile", "Phone Number", "WhatsApp", "contact"):
        r = sheet_row_to_canonical({col: "9876500001", "Name": "Asha"})
        assert r is not None
        assert r.phone_e164 == "+919876500001", (col, r.phone_e164)


def test_mapper_email_lowercased_and_name():
    r = sheet_row_to_canonical({"E-mail": "Buyer@Example.COM", "Full Name": "Asha K"})
    assert r is not None
    assert r.email == "buyer@example.com"
    assert r.display_name == "Asha K"


def test_mapper_no_anchor_returns_none():
    # Columns the writers don't persist → no identity → dropped at the mapper.
    assert sheet_row_to_canonical({"GST": "29ABCDE1234F1Z5", "City": "Pune"}) is None
    assert sheet_row_to_canonical({}) is None


# --- VT-487: a numeric sheet cell read as a FLOAT must not corrupt into a bad number -------------


@pytest.mark.parametrize(
    "raw",
    [
        "9.98886e+11",   # str-form scientific notation (openpyxl/gspread numericized a cell)
        9.98886e11,      # an actual float cell value
        998886123456.0,  # float with .0
        "+91998886.0",   # decimal artifact
    ],
)
def test_normalize_e164_rejects_float_corruption(raw):
    """The old digit-strip glued a scientific-notation mantissa+exponent into a plausible-but-WRONG
    number (the Twilio 21211 breach). It must now reject (None) so email/name anchor instead."""
    assert _normalize_e164(raw) is None


def test_sheet_float_phone_cell_does_not_corrupt():
    """A sheet row whose phone cell came in as a float (numeric column) yields phone_e164=None
    rather than a corrupted number — the row still anchors on name."""
    r = sheet_row_to_canonical({"Phone": 9.98886e11, "Name": "Asha"})
    assert r is not None
    assert r.phone_e164 is None  # corrupted float NOT coerced into a bad number
    assert r.display_name == "Asha"


def test_normalize_e164_coerces_clean_int():
    """A clean int cell coerces to str and normalizes — proves the coerce path, not just reject."""
    assert _normalize_e164(9876500001) == "+919876500001"


def test_mapper_email_only_anchor_no_phone():
    r = sheet_row_to_canonical({"email": "x@y.com"})
    assert r is not None
    assert r.phone_e164 is None
    assert r.email == "x@y.com"


# --- PURE: sale columns -------------------------------------------------------

def test_mapper_sale_amount_and_date():
    r = sheet_row_to_canonical(
        {"Phone": "9876500002", "Name": "Bina", "Amount": "₹1,250.00",
         "Order Date": "01/06/2026"}
    )
    assert r is not None
    assert len(r.sales) == 1
    assert r.sales[0].amount_paise == 125000
    assert r.sales[0].entry_date == date(2026, 6, 1)
    assert r.sales[0].confidence == 1.0


def test_mapper_iso_date_and_plain_amount():
    r = sheet_row_to_canonical(
        {"phone": "9876500004", "total": "499", "date": "2026-06-15"}
    )
    assert r.sales[0].amount_paise == 49900
    assert r.sales[0].entry_date == date(2026, 6, 15)


def test_mapper_contacts_only_no_sale():
    # A bare contact sheet (no amount/date column) → identity-only, empty sales.
    r = sheet_row_to_canonical({"Name": "Chetan", "Phone": "9876500005"})
    assert r is not None
    assert r.sales == ()


def test_mapper_amount_without_date_no_sale():
    # A sale needs BOTH amount and date — amount alone is not enough.
    r = sheet_row_to_canonical({"Phone": "9876500006", "Amount": "500"})
    assert r.sales == ()


def test_mapper_unparseable_amount_no_sale():
    r = sheet_row_to_canonical(
        {"Phone": "9876500007", "Amount": "N/A", "Date": "2026-06-01"}
    )
    assert r.sales == ()


def test_mapper_consent_always_none():
    # Sheets never carry lawful WhatsApp consent (option-A analog).
    r = sheet_row_to_canonical(
        {"Phone": "9876500008", "Amount": "100", "Date": "2026-06-01"}
    )
    assert r.consent is None


def test_mapper_drops_extra_columns():
    # PII boundary (§3): address / GST / notes columns are NOT read into the row.
    r = sheet_row_to_canonical(
        {"Phone": "9876500009", "Address": "12 MG Road", "GST": "29ABC",
         "Notes": "VIP — drop me"}
    )
    assert r is not None
    assert set(r.model_dump().keys()) == {
        "phone_e164", "email", "display_name", "sales", "consent"
    }


# --- PURE: GoogleSheetConnector.pull_full now surfaces row dicts ---------------

def test_pull_full_zips_header_into_row_dicts(monkeypatch):
    """The data-surfacing fix: pull_full returns {column -> cell} dicts (header
    row zipped onto each data row), not data-less envelopes."""
    from orchestrator.integrations.connectors import google_sheet as gs

    calls = {"n": 0}

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _fake_get(url, **kwargs):
        # First call = header (A1:Z1); second = data (A2:Z).
        calls["n"] += 1
        if "A1:Z1" in url:
            return _Resp({"values": [["Name", "Phone", "Amount", "Date"]]})
        return _Resp(
            {"values": [["Asha", "9876500001", "499.00", "2026-06-01"],
                        ["Bina", "9876500002", "250", "2026-06-02"]]}
        )

    monkeypatch.setattr(gs.httpx, "get", _fake_get)
    monkeypatch.setattr(
        gs.GoogleSheetConnector, "get_access_token",
        lambda self, tid: "fake-token",
    )

    rows = gs.GoogleSheetConnector().pull_full(uuid4(), "spreadsheet-abc")
    assert isinstance(rows, list)
    assert rows[0] == {"Name": "Asha", "Phone": "9876500001",
                       "Amount": "499.00", "Date": "2026-06-01"}
    # And the mapper can land them.
    mapped = sheet_row_to_canonical(rows[0])
    assert mapped.phone_e164 == "+919876500001"
    assert mapped.sales[0].amount_paise == 49900


# --- DB + CANARY (real Postgres) ----------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-417 PR-2 sheet DB/canary tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    if not os.environ.get("TEAM_PHONE_HASH_SALT"):
        os.environ["TEAM_PHONE_HASH_SALT"] = "vt417-pr2-canary-salt"
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
            "VALUES ('VT-417 PR-2 sheet ingest test', 'founding', 'onboarding') "
            "RETURNING id"
        ).fetchone()[0])


def _uniq_phone() -> str:
    return "+9190" + uuid4().int.__str__()[:8]


@_DB
def test_sheet_ingest_lands_customer_and_sale(db_ctx):
    """PROOF: a parsed sheet row (amount+date) → a real customers row + a real
    ``sale`` customer_ledger_entries row tagged acquired_via='google_sheet'."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.ingest import ingest_customer_rows

    tenant = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    row = sheet_row_to_canonical(
        {"Name": "Sheet Asha", "Phone": phone, "Amount": "₹499.00",
         "Date": "2026-06-01"}
    )
    assert row is not None
    summary = ingest_customer_rows(tenant, [row], acquired_via="google_sheet")
    assert (summary.committed, summary.sales_written) == (1, 1)

    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT id, display_name, phone_e164, acquired_via FROM customers "
            "WHERE tenant_id = %s AND phone_e164 = %s", (tenant, phone)
        ).fetchone()
        assert cust is not None
        assert cust["display_name"] == "Sheet Asha"
        assert "google_sheet" in (cust["acquired_via"] or [])
        led = conn.execute(
            "SELECT amount_paise, entry_type, entry_date, acquired_via "
            "FROM customer_ledger_entries WHERE tenant_id = %s AND customer_id = %s",
            (tenant, cust["id"])
        ).fetchall()
    assert len(led) == 1
    assert led[0]["amount_paise"] == 49900
    assert led[0]["entry_type"] == "sale"
    assert led[0]["entry_date"] == date(2026, 6, 1)
    assert led[0]["acquired_via"] == "google_sheet"


@_DB
def test_sheet_contacts_only_lands_identity_no_ledger(db_ctx):
    """A contacts sheet (no amount column) lands a customer with NO ledger row."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.ingest import ingest_customer_rows

    tenant = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    row = sheet_row_to_canonical({"Name": "Contacts Only", "Phone": phone})
    assert row is not None and row.sales == ()
    summary = ingest_customer_rows(tenant, [row], acquired_via="google_sheet")
    assert summary.committed == 1
    assert summary.sales_written == 0

    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT id FROM customers WHERE tenant_id = %s AND phone_e164 = %s",
            (tenant, phone)
        ).fetchone()
        assert cust is not None
        nled = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries "
            "WHERE tenant_id = %s AND customer_id = %s",
            (tenant, cust["id"])
        ).fetchone()["n"]
    assert nled == 0


@_DB
def test_sheet_push_canary_full_write(db_ctx):
    """CANARY — synthetic HMAC-signed sheet-push payload → the sheet_push handler
    → asserted real customers + ``sale`` ledger rows. The end-to-end inbound proof
    (Rule #15). HMAC over the raw body with the tenant's push_secret."""
    import asyncio
    import hashlib
    import hmac
    import json

    from orchestrator.api.sheet_push import sheet_push
    from orchestrator.db import tenant_connection

    tenant = _tenant(db_ctx.dsn)
    push_secret = "vt417_pr2_sheet_canary"  # gitleaks:allow — fake test secret for the HMAC canary, not a real credential
    with psycopg.connect(db_ctx.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_oauth_tokens "
            "(tenant_id, connector_id, refresh_token_encrypted, scopes, push_secret) "
            "VALUES (%s, 'google_sheet', 'enc', ARRAY['sheets.readonly'], %s)",
            (tenant, push_secret),
        )

    phone = _uniq_phone()
    payload = {
        "row_data": {
            "Name": "Canary Sheet", "Phone": phone,
            "Amount": "₹799.00", "Date": "2026-06-10",
            "Address": "DROP ME", "Notes": "DROP ME",
        }
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(push_secret.encode(), body, hashlib.sha256).hexdigest()

    class _Req:
        async def body(self):
            return body

        async def json(self):
            return payload

    out = asyncio.run(
        sheet_push(
            _Req(),  # type: ignore[arg-type]
            x_viabe_signature=sig,
            x_viabe_tenant=tenant,
        )
    )
    assert out["status"] == "ok"
    assert out["rows_committed"] == 1
    assert out["sales_written"] == 1

    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT id FROM customers WHERE tenant_id = %s AND phone_e164 = %s",
            (tenant, phone)
        ).fetchone()
        assert cust is not None, "canary: no customers row landed"
        led = conn.execute(
            "SELECT amount_paise, entry_type, acquired_via FROM "
            "customer_ledger_entries WHERE tenant_id = %s AND customer_id = %s",
            (tenant, cust["id"])
        ).fetchall()
        # PII boundary: Address/Notes columns never persisted (no such columns).
    assert len(led) == 1
    assert led[0]["amount_paise"] == 79900       # 799.00 INR → paise
    assert led[0]["entry_type"] == "sale"
    assert led[0]["acquired_via"] == "google_sheet"


@_DB
def test_sheet_push_redelivery_idempotent(db_ctx):
    """Same sheet row re-pushed → NO duplicate customer, NO double-count ledger."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.ingest import ingest_customer_rows

    tenant = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    row = sheet_row_to_canonical(
        {"Name": "Retry Sheet", "Phone": phone, "Amount": "123.00",
         "Date": "2026-06-02"}
    )
    s1 = ingest_customer_rows(tenant, [row], acquired_via="google_sheet")
    s2 = ingest_customer_rows(tenant, [row], acquired_via="google_sheet")
    assert s1.sales_written == 1
    assert (s2.sales_written, s2.sales_skipped_duplicate) == (0, 1)

    with tenant_connection(tenant) as conn:
        ncust = conn.execute(
            "SELECT count(*) AS n FROM customers WHERE tenant_id = %s AND "
            "phone_e164 = %s", (tenant, phone)
        ).fetchone()["n"]
        nled = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries WHERE "
            "tenant_id = %s", (tenant,)
        ).fetchone()["n"]
    assert ncust == 1, "re-delivery duplicated the customer"
    assert nled == 1, "re-delivery double-counted the sale"
