"""VT-57/58/59 + VT-275 — imported_transactions raw-import write path.

PURE: ImportedTxnIn validation. DB (real Postgres, no mock cursors): attributed
credit → BOTH surfaces; unattributed → imported only; idempotent re-import (N2:
no double-count on either surface); attributed DEBIT/refund → retained raw, NOT
promoted (N1); cross-tenant isolation. This is the DR-15 canary for migration 062.
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.imported_transactions import ImportedTxnIn  # noqa: E402


# --- PURE ---------------------------------------------------------------------

def test_defaults():
    t = ImportedTxnIn(provider_ref="r1", amount_paise=5000,
                      txn_date=date(2026, 6, 1), direction="credit")
    assert t.customer_id is None and t.entry_type == "sale" and t.confidence == 0.95


def test_negative_amount_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImportedTxnIn(provider_ref="r", amount_paise=-1,
                      txn_date=date(2026, 6, 1), direction="credit")


def test_bad_direction_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImportedTxnIn(provider_ref="r", amount_paise=1,
                      txn_date=date(2026, 6, 1), direction="sideways")


def test_unknown_acquired_via_raises():
    from orchestrator.integrations.dedup_merge import AcquiredViaError
    from orchestrator.integrations.imported_transactions import (
        record_imported_transactions,
    )

    with pytest.raises(AcquiredViaError):
        record_imported_transactions(uuid4(), [], acquired_via="not_a_method")


# --- DB (real Postgres) -------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — imported_transactions DB tests skipped",
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
            "VALUES ('VT-062 imp-txn test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _customer(tenant: str) -> str:
    from orchestrator.integrations.dedup_merge import dedup_and_merge

    phone = "+9190" + uuid4().int.__str__()[:8]
    m = dedup_and_merge(tenant, acquired_via="kot_pos", phone_e164=phone,
                        display_name="Imp Test")
    assert m.customer_id is not None
    return str(m.customer_id)


def _counts(tenant: str) -> tuple[int, int]:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        imp = conn.execute(
            "SELECT count(*) AS n FROM imported_transactions").fetchone()["n"]
        led = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
    return imp, led


@_DB
def test_attributed_credit_writes_both_surfaces(db_ctx):
    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    t = _tenant(db_ctx.dsn)
    cid = _customer(t)
    res = record_imported_transactions(t, [ImportedTxnIn(
        provider_ref="bill-1", amount_paise=80000, txn_date=date(2026, 6, 1),
        direction="credit", customer_id=cid)], acquired_via="kot_pos")
    assert res.written == 1 and res.attributed_ledger_written == 1
    assert _counts(t) == (1, 1)  # imported_transactions + customer_ledger_entries


@_DB
def test_unattributed_imported_only(db_ctx):
    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    t = _tenant(db_ctx.dsn)
    res = record_imported_transactions(t, [ImportedTxnIn(
        provider_ref="bill-2", amount_paise=50000, txn_date=date(2026, 6, 1),
        direction="credit")], acquired_via="kot_pos")  # customer_id None
    assert res.written == 1 and res.attributed_ledger_written == 0
    assert _counts(t) == (1, 0)  # raw only, no ledger


@_DB
def test_reimport_idempotent_both_surfaces(db_ctx):
    """N2: re-import is a no-op on BOTH surfaces — no double-count."""
    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    t = _tenant(db_ctx.dsn)
    cid = _customer(t)
    row = ImportedTxnIn(provider_ref="bill-3", amount_paise=70000,
                        txn_date=date(2026, 6, 1), direction="credit", customer_id=cid)
    record_imported_transactions(t, [row], acquired_via="kot_pos")
    res2 = record_imported_transactions(t, [row], acquired_via="kot_pos")  # again
    assert res2.written == 0 and res2.skipped_duplicate == 1
    assert res2.attributed_ledger_written == 0
    assert _counts(t) == (1, 1)  # still one each — no double-count


@_DB
def test_attributed_debit_retained_not_promoted(db_ctx):
    """N1: a refund (debit) is RETAINED raw but NOT written to the ledger."""
    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    t = _tenant(db_ctx.dsn)
    cid = _customer(t)
    res = record_imported_transactions(t, [ImportedTxnIn(
        provider_ref="refund-1", amount_paise=30000, txn_date=date(2026, 6, 1),
        direction="debit", customer_id=cid)], acquired_via="kot_pos")
    assert res.written == 1 and res.attributed_ledger_written == 0
    assert _counts(t) == (1, 0)  # refund parked raw, ledger untouched


@_DB
def test_cross_tenant_isolation(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    a, b = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    record_imported_transactions(a, [ImportedTxnIn(
        provider_ref="bill-x", amount_paise=10000, txn_date=date(2026, 6, 1),
        direction="credit")], acquired_via="kot_pos")
    with tenant_connection(b) as conn:
        seen = conn.execute(
            "SELECT count(*) AS n FROM imported_transactions").fetchone()["n"]
    assert seen == 0, "tenant B saw tenant A's imports (RLS breach)"
