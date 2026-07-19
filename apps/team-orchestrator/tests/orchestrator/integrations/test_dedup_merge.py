"""VT-54 — dedup + merge tests.

PURE: enum rejection (the "gate" — invalid acquired_via REJECTED) + eligibility.
DB: insert / 2-method merge / non-overwrite / confidence-gate / ambiguous /
cross-tenant RLS, against live Postgres (DATABASE_URL), run in the CI
``orchestrator`` job. Real DB, no mock cursors (VT-263 / Cowork VT-54 bar).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.dedup_merge import (  # noqa: E402
    ACQUIRED_VIA,
    AcquiredViaError,
    dedup_and_merge,
)


# --- PURE: enum gate (invalid tag REJECTED before any DB work) ----------------

def test_invalid_acquired_via_rejected():
    assert "totally_made_up" not in ACQUIRED_VIA
    with pytest.raises(AcquiredViaError):
        dedup_and_merge(
            "11111111-1111-4111-8111-111111111111",
            acquired_via="totally_made_up", phone_e164="+919000000001",
        )


def test_enum_has_all_vt6_methods():
    # Single-source enum must carry the 13 VT-6 methods + the VT-417 inbound
    # connector lineage (shopify / google_sheet / drive_sheet).
    assert ACQUIRED_VIA == {
        "paper_book", "contacts", "upi_phonepe", "upi_gpay", "upi_paytm",
        "kot_pos", "cash_book", "qr_opt_in", "apify_zomato", "apify_swiggy",
        "apify_magicpin", "apify_gbp", "owner_typed",
        # VT-417 inbound connector lineage.
        "shopify", "google_sheet", "drive_sheet",
    }


# --- DB: persistence + merge semantics + RLS ----------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — dedup-merge DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    # dedup_and_merge registers the privacy-preserving phone_token on insert
    # (VT-191 encrypt-at-rest). Provide a valid Fernet key so the REAL register
    # path runs (prod/dev supply this via .viabe/secrets; CI env doesn't set it).
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-54 dedup test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    return str(row[0])


def _uniq_phone() -> str:
    # Synthetic, unique per test (avoid cross-test UNIQUE collisions).
    return "+9190" + uuid4().int.__str__()[:8]


@_DB
def test_insert_then_two_method_merge(db_ctx):
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(db_ctx.dsn)
    phone = _uniq_phone()

    r1 = dedup_and_merge(tenant, acquired_via="paper_book", phone_e164=phone,
                         display_name="Asha")
    assert r1.kind == "inserted"

    r2 = dedup_and_merge(tenant, acquired_via="contacts", phone_e164=phone)
    assert r2.kind == "merged"
    assert r2.customer_id == r1.customer_id  # same row
    assert r2.acquired_via == ("contacts", "paper_book")  # appended + sorted

    # Exactly ONE customers row for that phone.
    with tenant_connection(tenant) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM customers WHERE phone_e164 = %s", (phone,)
        ).fetchone()["n"]
    assert n == 1


@_DB
def test_merge_is_non_overwrite(db_ctx):
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(db_ctx.dsn)
    phone = _uniq_phone()
    dedup_and_merge(tenant, acquired_via="paper_book", phone_e164=phone,
                    display_name="Asha")
    # Incoming a DIFFERENT name for the same phone -> existing name kept.
    dedup_and_merge(tenant, acquired_via="contacts", phone_e164=phone,
                    display_name="Different Name")
    with tenant_connection(tenant) as conn:
        name = conn.execute(
            "SELECT display_name FROM customers WHERE phone_e164 = %s", (phone,)
        ).fetchone()["display_name"]
    assert name == "Asha", "merge overwrote an existing non-NULL field"


@_DB
def test_ask_level_confidence_not_committed(db_ctx):
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(db_ctx.dsn)
    phone = _uniq_phone()
    # display_name confidence 0.5 (< 0.7 ask) -> NOT committed (P4 / VT-53 routes it).
    r = dedup_and_merge(tenant, acquired_via="owner_typed", phone_e164=phone,
                        display_name="LowConf", field_confidences={"display_name": 0.5})
    assert r.kind == "inserted"
    with tenant_connection(tenant) as conn:
        name = conn.execute(
            "SELECT display_name FROM customers WHERE id = %s", (str(r.customer_id),)
        ).fetchone()["display_name"]
    assert name is None, "ask-level field was committed despite <0.7 confidence"


@_DB
def test_ambiguous_parks_not_merges(db_ctx):
    tenant = _new_tenant(db_ctx.dsn)
    phone = _uniq_phone()
    email = f"{uuid4().hex[:8]}@synthetic.test"
    # Customer A: phone only. Customer B: email only.
    a = dedup_and_merge(tenant, acquired_via="paper_book", phone_e164=phone)
    b = dedup_and_merge(tenant, acquired_via="contacts", email=email)
    assert a.kind == "inserted" and b.kind == "inserted"
    # Incoming carrying BOTH -> matches A (phone) AND B (email) -> ambiguous.
    r = dedup_and_merge(tenant, acquired_via="upi_gpay", phone_e164=phone, email=email)
    assert r.kind == "ambiguous"
    assert r.customer_id is None
    assert r.pending_dedup_id is not None


@_DB
def test_cross_tenant_isolation(db_ctx):
    from orchestrator.db import tenant_connection

    tenant_a = _new_tenant(db_ctx.dsn)
    tenant_b = _new_tenant(db_ctx.dsn)
    phone = _uniq_phone()
    ra = dedup_and_merge(tenant_a, acquired_via="paper_book", phone_e164=phone)
    assert ra.kind == "inserted"
    # B merging the SAME phone must NOT see A's row -> a fresh insert under B
    # (real count backstop: B sees 0 of A's customer id).
    rb = dedup_and_merge(tenant_b, acquired_via="contacts", phone_e164=phone)
    assert rb.kind == "inserted"
    assert rb.customer_id != ra.customer_id
    with tenant_connection(tenant_b) as conn:
        b_sees_a = conn.execute(
            "SELECT count(*) AS n FROM customers WHERE id = %s",
            (str(ra.customer_id),),
        ).fetchone()["n"]
    assert b_sees_a == 0, "RLS leak: tenant B saw tenant A's customer"
