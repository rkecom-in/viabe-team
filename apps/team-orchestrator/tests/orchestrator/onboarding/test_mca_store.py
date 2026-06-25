"""VT-449 / VT-411 — MCA company-master persistence + tier-2 ownership. Real-PG canary (synthetic; CL-422).

Proves: store_company_master_data UPSERTs a tenant_mca_data row with the PLAIN registry facts +
NON-EMPTY ciphertext for the two PII fields (registered_address, directors[]) — and that the
plaintext address / director name does NOT appear in the stored ciphertext columns (encryption is
real, not a passthrough). set_owner_channel_verified flips the tenants tier-2 flag + stamps the time.
UPSERT idempotency (second store overwrites, stays one row).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

# A real Fernet key so encrypt_value works in-test (mca_store defers the import). Synthetic key.
_DIRECTOR_NAME = "RAJESH KUMAR SHARMA"
_ADDRESS = "12B MG ROAD, FORT, MUMBAI 400001"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations
    from cryptography.fernet import Fernet

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _cmd(**over):  # type: ignore[no-untyped-def]
    from orchestrator.integrations.methods.mca import CompanyMasterData

    base = dict(
        ok=True,
        cin="U72900MH2020PTC123456",
        company_name="ACME TRADERS PRIVATE LIMITED",
        status="ACTV",
        active_compliance="Active Compliant",
        class_of_company="Private",
        company_category="Company limited by shares",
        registered_address=_ADDRESS,
        roc_code="RoC-Mumbai",
        date_of_incorporation="2020-01-15",
        paid_up_capital="100000",
        authorised_capital="1000000",
        directors=({"name": _DIRECTOR_NAME, "din": "01234567", "designation": "Director"},),
    )
    base.update(over)
    return CompanyMasterData(**base)


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, owner_phone) "
            "VALUES ('Acme Traders', 'founding', 'trial', %s, %s) RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}", f"+9188{uuid4().int % 10**8:08d}"),
        ).fetchone()[0])


def _mca_row(dsn, tid) -> dict:
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(
            "SELECT cin, company_name, status, active_compliance, class_of_company, "
            "company_category, roc_code, date_of_incorporation, paid_up_capital, authorised_capital, "
            "registered_address_encrypted, directors_encrypted "
            "FROM tenant_mca_data WHERE tenant_id = %s", (tid,)
        ).fetchone()


def test_store_persists_plain_facts_and_encrypts_pii(substrate):
    from orchestrator.observability.encrypt_value import decrypt_value
    from orchestrator.onboarding.mca_store import store_company_master_data

    tid = _tenant(substrate)
    store_company_master_data(tid, _cmd())

    r = _mca_row(substrate, tid)
    assert r is not None
    # Plain (non-PII) registry facts stored verbatim.
    assert r["cin"] == "U72900MH2020PTC123456"
    assert r["company_name"] == "ACME TRADERS PRIVATE LIMITED"
    assert r["status"] == "ACTV"
    assert r["active_compliance"] == "Active Compliant"
    assert r["class_of_company"] == "Private"
    assert r["company_category"] == "Company limited by shares"
    assert r["roc_code"] == "RoC-Mumbai"
    assert r["date_of_incorporation"] == "2020-01-15"
    assert r["paid_up_capital"] == "100000"
    assert r["authorised_capital"] == "1000000"

    # PII fields are NON-EMPTY ciphertext, and the plaintext is NOT present at rest.
    addr_ct = r["registered_address_encrypted"]
    dirs_ct = r["directors_encrypted"]
    assert addr_ct and dirs_ct
    assert _ADDRESS not in addr_ct  # encryption is real, not a passthrough
    assert _DIRECTOR_NAME not in dirs_ct
    # And decrypts back to the original (round-trip).
    assert decrypt_value(addr_ct) == _ADDRESS
    assert _DIRECTOR_NAME in decrypt_value(dirs_ct)


def test_store_is_upsert_idempotent(substrate):
    from orchestrator.onboarding.mca_store import store_company_master_data

    tid = _tenant(substrate)
    store_company_master_data(tid, _cmd(status="ACTV"))
    store_company_master_data(tid, _cmd(status="STRK"))  # second store overwrites
    with psycopg.connect(substrate, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT status FROM tenant_mca_data WHERE tenant_id = %s", (tid,)
        ).fetchall()
    assert len(rows) == 1  # ON CONFLICT (tenant_id) — one row per tenant
    assert rows[0]["status"] == "STRK"


def test_set_owner_channel_verified_flips_flag(substrate):
    from orchestrator.onboarding.mca_store import set_owner_channel_verified

    tid = _tenant(substrate)
    with psycopg.connect(substrate, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        before = conn.execute(
            "SELECT owner_channel_verified, owner_channel_verified_at FROM tenants WHERE id = %s", (tid,)
        ).fetchone()
    assert before["owner_channel_verified"] is False
    assert before["owner_channel_verified_at"] is None

    set_owner_channel_verified(tid)

    with psycopg.connect(substrate, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        after = conn.execute(
            "SELECT owner_channel_verified, owner_channel_verified_at FROM tenants WHERE id = %s", (tid,)
        ).fetchone()
    assert after["owner_channel_verified"] is True
    assert after["owner_channel_verified_at"] is not None
