"""VT-56 — phone contacts import tests.

PURE: vCard/CSV parsing + phone normalization + ambiguous-CSV error (no DB).
DB: ingest_contacts → committed identity rows (acquired_via=contacts) / non-Indian
phone → clarification / cross-tenant, against real Postgres. No mock cursors.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.methods.contacts import (  # noqa: E402
    ContactsParseError,
    _normalize_phone,
    _parse_csv,
    _parse_vcard,
)

_VCF = """BEGIN:VCARD
VERSION:3.0
FN:Asha Devi
TEL;TYPE=CELL:9000000001
END:VCARD
BEGIN:VCARD
FN:Ravi Kumar
TEL:+919000000002
END:VCARD
BEGIN:VCARD
FN:No Phone Person
END:VCARD
"""


# --- PURE ---------------------------------------------------------------------

def test_parse_vcard_skips_no_phone():
    out = _parse_vcard(_VCF)
    assert len(out) == 2  # the FN-only card is dropped
    assert out[0]["name"] == "Asha Devi" and out[0]["phone"] == "9000000001"


def test_parse_csv_name_phone():
    csv_text = "Name,Phone Number\nAsha,9000000001\nRavi,+919000000002\n"
    out = _parse_csv(csv_text)
    assert len(out) == 2 and out[0]["name"] == "Asha"


def test_parse_csv_first_last_name():
    csv_text = "First Name,Last Name,Mobile\nAsha,Devi,9000000001\n"
    out = _parse_csv(csv_text)
    assert out[0]["name"] == "Asha Devi"


def test_parse_csv_ambiguous_columns_raises():
    with pytest.raises(ContactsParseError):
        _parse_csv("foo,bar\n1,2\n")  # no phone-ish column


@pytest.mark.parametrize(
    "raw,e164,is_high",
    [
        ("9000000001", "+919000000001", True),
        ("+91 90000 00002", "+919000000002", True),
        ("090000 00003", "+919000000003", True),
        ("+1 415 555 0001", "+14155550001", False),   # foreign → low conf
        ("", None, False),
        ("abc", None, False),
    ],
)
def test_normalize_phone(raw, e164, is_high):
    out, conf = _normalize_phone(raw)
    assert out == e164
    if e164 is not None:
        assert (conf >= 0.85) == is_high


# --- DB -----------------------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — contacts DB tests skipped",
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
            "VALUES ('VT-56 contacts test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _u() -> str:
    return uuid4().int.__str__()[:9]


@_DB
def test_vcard_import_commits_identity(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.contacts import ingest_contacts

    tenant = _tenant(db_ctx.dsn)
    a, b = _u()[:9], _u()[:9]
    vcf = (f"BEGIN:VCARD\nFN:Asha\nTEL:90{a[:8]}\nEND:VCARD\n"
           f"BEGIN:VCARD\nFN:Ravi\nTEL:90{b[:8]}\nEND:VCARD\n")
    summary = ingest_contacts(tenant, vcf.encode())
    assert summary.committed == 2 and summary.pending_clarification == 0
    with tenant_connection(tenant) as conn:
        tags = [r["acquired_via"] for r in conn.execute(
            "SELECT acquired_via FROM customers").fetchall()]
    assert all(t == ["contacts"] for t in tags) and len(tags) == 2


@_DB
def test_foreign_number_routes_to_clarification(db_ctx):
    from orchestrator.integrations.methods.contacts import ingest_contacts

    tenant = _tenant(db_ctx.dsn)
    vcf = "BEGIN:VCARD\nFN:Foreigner\nTEL:+1 415 555 0001\nEND:VCARD\n"
    summary = ingest_contacts(tenant, vcf.encode())
    # foreign phone → low conf → clarification, not silent commit.
    assert summary.committed == 0 and summary.pending_clarification == 1


@_DB
def test_cross_tenant_contacts_isolated(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.contacts import ingest_contacts

    ta, tb = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    digits = _u()[:8]
    ingest_contacts(ta, f"BEGIN:VCARD\nFN:A\nTEL:90{digits}\nEND:VCARD\n".encode())
    with tenant_connection(tb) as conn:
        n = conn.execute("SELECT count(*) AS n FROM customers").fetchone()["n"]
    assert n == 0, "RLS leak: tenant B sees tenant A's imported contacts"
