"""VT-267 PR-A2 — tenant provisioning (D1 identity). Real Postgres, no mock cursors.

business_contact (WhatsApp) = unique tenant identity (mig 066); same number → same
tenant (merge); owner_contact optional/nullable + backfills.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.onboarding.tenant_provision import create_tenant_if_unknown  # noqa: E402


def test_empty_business_contact_raises():
    with pytest.raises(ValueError, match="mandatory"):
        create_tenant_if_unknown("")


pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — tenant_provision DB tests skipped",
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


def _num() -> str:
    return "+9190" + uuid4().int.__str__()[:8]


def _row(dsn: str, num: str):
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT id::text AS id, phase, owner_contact, created_via, business_name "
            "FROM tenants WHERE whatsapp_number = %s", (num,)
        ).fetchall()


@_DB
def test_create_new_then_merge_same_number(db_ctx):
    num = _num()
    r1 = create_tenant_if_unknown(num, business_name="Asha Store", created_via="whatsapp")
    assert r1.created is True
    # Same business_contact → SAME tenant (merge, not new).
    r2 = create_tenant_if_unknown(num)
    assert r2.created is False and r2.tenant_id == r1.tenant_id
    # Exactly one row for the number (unique identity enforced).
    rows = _row(db_ctx.dsn, num)
    assert len(rows) == 1 and rows[0]["phase"] == "onboarding"
    assert rows[0]["created_via"] == "whatsapp" and rows[0]["business_name"] == "Asha Store"


@_DB
def test_owner_contact_optional_and_backfills(db_ctx):
    num = _num()
    r1 = create_tenant_if_unknown(num)  # no owner_contact
    assert _row(db_ctx.dsn, num)[0]["owner_contact"] is None  # nullable
    owner = "+9199" + uuid4().int.__str__()[:8]
    r2 = create_tenant_if_unknown(num, owner_contact=owner)  # backfill
    assert r2.tenant_id == r1.tenant_id
    assert _row(db_ctx.dsn, num)[0]["owner_contact"] == owner


@_DB
def test_distinct_numbers_distinct_tenants(db_ctx):
    a = create_tenant_if_unknown(_num())
    b = create_tenant_if_unknown(_num())
    assert a.tenant_id != b.tenant_id and a.created and b.created


@_DB
def test_default_business_name_is_the_number(db_ctx):
    num = _num()
    create_tenant_if_unknown(num)  # no business_name
    assert _row(db_ctx.dsn, num)[0]["business_name"] == num  # placeholder
