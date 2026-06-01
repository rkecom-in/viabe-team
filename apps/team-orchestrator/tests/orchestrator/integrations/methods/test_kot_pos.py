"""VT-58 — KOT/POS export tests.

PURE: row→entry mapping + CSV/JSON parse (no DB). DB: attributed row → customer +
ledger; unattributed (no phone/name) → counted/dropped, NOT persisted (deferred to
imported_transactions); idempotent re-ingest. Real Postgres, no mock cursors.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.methods.kot_pos import _parse_records, _row_to_entry  # noqa: E402


# --- PURE ---------------------------------------------------------------------

def test_row_without_amount_is_none():
    assert _row_to_entry({"customer": "Asha", "phone": "9000000001"}) is None


def test_row_maps_amount_date_phone_name():
    e = _row_to_entry({"Total": "1500", "Bill Date": "2026-06-01",
                       "Customer Phone": "9000000001", "Customer Name": "Asha"})
    names = {f.name: f.value for f in e.fields}
    # phone is normalized to E.164 (shared with contacts) for cross-method dedup.
    assert names == {"amount": "1500", "entry_date": "2026-06-01",
                     "phone": "+919000000001", "customer_name": "Asha"}


def test_parse_csv_and_json():
    csv_text = "Total,Phone\n1500,9000000001\n,9000000002\n2500,\n"
    rows = _parse_records(csv_text, "csv")
    assert len(rows) == 2  # the amount-less middle row dropped
    j = json.dumps([{"amount": "100"}, {"foo": "bar"}])
    assert len(_parse_records(j, "json")) == 1


# --- DB -----------------------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — kot_pos DB tests skipped",
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
            "VALUES ('VT-58 kot test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _phone() -> str:
    return "90" + uuid4().int.__str__()[:8]


@_DB
def test_attributed_row_commits_customer_and_ledger(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.kot_pos import ingest_kot_pos

    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    csv_text = f"Total,Bill Date,Customer Phone\n1500,2026-06-01,{phone}\n"
    summary = ingest_kot_pos(tenant, csv_text.encode(), "csv")
    assert summary.committed == 1
    with tenant_connection(tenant) as conn:
        c = conn.execute("SELECT count(*) AS n FROM customers WHERE phone_e164=%s",
                         ("+91" + phone,)).fetchone()["n"]
        led = conn.execute("SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
    assert c == 1 and led == 1


@_DB
def test_unattributed_row_dropped_not_persisted(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.kot_pos import ingest_kot_pos

    tenant = _tenant(db_ctx.dsn)
    # No phone/name → unattributed → counted dropped, NOT persisted.
    summary = ingest_kot_pos(tenant, b"Total,Bill Date\n1500,2026-06-01\n", "csv")
    assert summary.committed == 0 and summary.dropped == 1
    with tenant_connection(tenant) as conn:
        led = conn.execute("SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
        cust = conn.execute("SELECT count(*) AS n FROM customers").fetchone()["n"]
    assert led == 0 and cust == 0


@_DB
def test_reingest_idempotent(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.kot_pos import ingest_kot_pos

    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    csv_text = f"Total,Bill Date,Customer Phone\n1500,2026-06-01,{phone}\n"
    ingest_kot_pos(tenant, csv_text.encode(), "csv")
    ingest_kot_pos(tenant, csv_text.encode(), "csv")  # re-ingest same export
    with tenant_connection(tenant) as conn:
        led = conn.execute("SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
    assert led == 1, "re-ingest duplicated ledger rows (entry_key idempotency)"
