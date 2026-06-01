"""VT-55 — paper-book adapter tests.

Vision is INJECTED (canned entries) so no network; dedup + ledger + clarifying
hit REAL Postgres (DATABASE_URL), run in the CI orchestrator job. No mock cursors.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.vision_extraction import (  # noqa: E402
    ExtractedField,
    ExtractionResult,
)


def _entry(**fields) -> ExtractionResult:
    # fields: name -> (value, confidence)
    return ExtractionResult(
        fields=tuple(
            ExtractedField(name=n, value=v, confidence=c) for n, (v, c) in fields.items()
        ),
        acquired_via="paper_book", model="test",
    )


pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — paper_book DB tests skipped",
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
            "VALUES ('VT-55 paper-book test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _phone() -> str:
    return "+9190" + uuid4().int.__str__()[:8]


@_DB
def test_high_confidence_commits_customer_and_ledger(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.paper_book import ingest_paper_book

    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    entries = [_entry(
        customer_name=("Asha", 0.93), phone=(phone, 0.95),
        amount=("1500", 0.9), entry_date=("2026-06-01", 0.9),
    )]
    summary = ingest_paper_book(tenant, b"img", extract_fn=lambda *a, **k: entries)
    assert (summary.entries_extracted, summary.committed) == (1, 1)
    assert summary.pending_clarification == 0 and summary.dropped == 0
    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT count(*) AS n FROM customers WHERE phone_e164 = %s", (phone,)
        ).fetchone()["n"]
        # ₹1500 → 150000 paise persisted.
        led = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries WHERE amount_paise = 150000"
        ).fetchone()["n"]
    assert cust == 1 and led >= 1


@_DB
def test_low_confidence_routes_to_clarification(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.paper_book import ingest_paper_book

    tenant = _tenant(db_ctx.dsn)
    entries = [_entry(
        customer_name=("Blurry", 0.55), phone=(_phone(), 0.95),
    )]
    summary = ingest_paper_book(tenant, b"img", extract_fn=lambda *a, **k: entries)
    assert (summary.committed, summary.pending_clarification) == (0, 1)
    with tenant_connection(tenant) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM pending_clarifications WHERE status='pending'"
        ).fetchone()["n"]
    assert n == 1


@_DB
def test_no_anchor_entry_dropped(db_ctx):
    from orchestrator.integrations.methods.paper_book import ingest_paper_book

    tenant = _tenant(db_ctx.dsn)
    # All identity fields null/absent + nothing low-conf → nothing to anchor → drop.
    entries = [_entry(amount=("100", 0.9))]
    summary = ingest_paper_book(tenant, b"img", extract_fn=lambda *a, **k: entries)
    assert summary.dropped == 1 and summary.committed == 0


@_DB
def test_acquired_via_tag_is_paper_book(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.paper_book import ingest_paper_book

    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    entries = [_entry(customer_name=("Ravi", 0.9), phone=(phone, 0.95))]
    ingest_paper_book(tenant, b"img", extract_fn=lambda *a, **k: entries)
    with tenant_connection(tenant) as conn:
        acq = conn.execute(
            "SELECT acquired_via FROM customers WHERE phone_e164 = %s", (phone,)
        ).fetchone()["acquired_via"]
    assert acq == ["paper_book"]
