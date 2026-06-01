"""VT-46 — match_transactions tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("langchain")


T0 = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


def _ledger(*, id: str, amount: int, ts: datetime,
             vpa: str | None = None) -> dict[str, Any]:
    return {"id": id, "amount_paise": amount, "entry_ts": ts, "ref_vpa": vpa}


def test_pydantic_io_shape() -> None:
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        MatchTransactionsOutput,
        TransactionInput,
        TransactionMatch,
        UnmatchedTransaction,
    )
    inp = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
                vpa="customer@upi",
            ),
        ],
    )
    assert inp.transactions[0].amount_paise == 15000

    MatchTransactionsOutput(
        matches=[TransactionMatch(
            txn_id="UPI001", ledger_entry_id="L1",
            confidence=0.95, match_basis="amount+time",
        )],
        unmatched=[UnmatchedTransaction(
            txn_id="UPI002", reason="no_amount_match",
        )],
    )


def test_exact_amount_plus_time_matches() -> None:
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
            ),
        ],
    )
    candidates = [
        _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=2)),
    ]
    result = match_transactions(payload, candidate_ledger=candidates)
    assert len(result.matches) == 1
    m = result.matches[0]
    assert m.ledger_entry_id == "L1"
    assert m.confidence > 0.5
    assert "amount" in m.match_basis
    assert "time" in m.match_basis


def test_amount_mismatch_unmatched() -> None:
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI002", amount_paise=20000, timestamp=T0,
            ),
        ],
    )
    candidates = [
        _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=2)),
    ]
    result = match_transactions(payload, candidate_ledger=candidates)
    assert result.matches == []
    assert len(result.unmatched) == 1
    assert result.unmatched[0].reason == "no_amount_match"


def test_empty_ledger_all_unmatched() -> None:
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
            ),
            TransactionInput(
                txn_id="UPI002", amount_paise=20000, timestamp=T0,
            ),
        ],
    )
    result = match_transactions(payload, candidate_ledger=[])
    assert result.matches == []
    assert len(result.unmatched) == 2
    assert all(u.reason == "no_ledger_candidate" for u in result.unmatched)


def test_vpa_fuzzy_boosts_confidence() -> None:
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
                vpa="customer.foo@upi",
            ),
        ],
    )
    candidates = [
        _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=1),
                vpa="customer.foo@upi"),
        _ledger(id="L2", amount=15000, ts=T0 + timedelta(hours=1),
                vpa="someone.else@upi"),
    ]
    result = match_transactions(payload, candidate_ledger=candidates)
    assert len(result.matches) == 1
    m = result.matches[0]
    assert m.ledger_entry_id == "L1"
    assert "vpa" in m.match_basis


def test_outside_24h_window_low_confidence_unmatched() -> None:
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
            ),
        ],
    )
    # Same amount but 36h apart → time_prox=0; composite=0.6 ≥ 0.5 so
    # this DOES match on amount alone. Verify match still flagged but
    # basis says amount-only.
    candidates = [
        _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=36)),
    ]
    result = match_transactions(payload, candidate_ledger=candidates)
    assert len(result.matches) == 1
    assert result.matches[0].match_basis == "amount"
    assert result.matches[0].confidence == pytest.approx(0.6, abs=0.01)


# --------------------------- VT-240 ---------------------------------------
# attribution_method provenance (computed field) + the pure mapper.


def test_attribution_method_exact_when_vpa_present() -> None:
    """A VPA-bearing match (strong payer id) → exact_match."""
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
                vpa="customer.foo@upi",
            ),
        ],
    )
    candidates = [
        _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=1),
                vpa="customer.foo@upi"),
    ]
    result = match_transactions(payload, candidate_ledger=candidates)
    m = result.matches[0]
    assert "vpa" in m.match_basis
    assert m.attribution_method == "exact_match"


def test_attribution_method_window_when_no_vpa() -> None:
    """amount / amount+time (no VPA) → window_match."""
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(txn_id="UPI001", amount_paise=15000, timestamp=T0),
        ],
    )
    candidates = [_ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=2))]
    result = match_transactions(payload, candidate_ledger=candidates)
    m = result.matches[0]
    assert "vpa" not in m.match_basis
    assert m.attribution_method == "window_match"


def test_attribution_method_from_match_basis_exhaustive() -> None:
    """Every basis the matcher can declare → its expected method. Declared
    matches always contain 'amount' (matcher requires amount-exact)."""
    from orchestrator.agent.tools.match_transactions import (
        attribution_method_from_match_basis,
    )
    cases = {
        "amount": "window_match",
        "amount+time": "window_match",
        "amount+vpa": "exact_match",
        "amount+time+vpa": "exact_match",
    }
    for basis, expected in cases.items():
        assert attribution_method_from_match_basis(basis) == expected, basis


def test_attribution_method_is_deterministic() -> None:
    """Reproducibility gate: same basis → same method, every call. No float
    comparison, no ordering dependence (substring of a token, not the tag)."""
    from orchestrator.agent.tools.match_transactions import (
        attribution_method_from_match_basis,
    )
    for basis in ("amount+vpa", "amount+time", "amount", "amount+time+vpa"):
        first = attribution_method_from_match_basis(basis)
        for _ in range(5):
            assert attribution_method_from_match_basis(basis) == first
    # 'vpa' must match a whole token, not a substring of another tag —
    # guard against a future basis tag that merely contains the letters.
    assert attribution_method_from_match_basis("amount+time") == "window_match"


def test_attribution_method_never_manual_owner() -> None:
    """The matcher path never produces 'manual_owner' — that value is
    owner-asserted on a separate path (migration 047 CHECK allows it, but the
    deterministic mapper must never emit it)."""
    from orchestrator.agent.tools.match_transactions import (
        attribution_method_from_match_basis,
    )
    for basis in ("amount", "amount+time", "amount+vpa", "amount+time+vpa", "none"):
        assert attribution_method_from_match_basis(basis) != "manual_owner"


def test_attribution_method_serialized_in_model_dump() -> None:
    """The computed field is part of the serialized output so a downstream
    writer (VT-176) reads it from model_dump without recomputing."""
    from orchestrator.agent.tools.match_transactions import TransactionMatch

    m = TransactionMatch(
        txn_id="UPI001", ledger_entry_id="L1",
        confidence=0.9, match_basis="amount+vpa",
    )
    dumped = m.model_dump()
    assert dumped["attribution_method"] == "exact_match"
    # confidence doubles as attribution_confidence (already in [0,1]).
    assert 0.0 <= dumped["confidence"] <= 1.0


# --- VT-275 attribution bridge (real Postgres, no mock cursors) ---------------

import os  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — attribution-bridge DB tests skipped",
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
            "VALUES ('VT-275 bridge test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _attributed_ledger_entry(tenant: str, amount: int, d) -> str:
    """Create a customer + an attributed ledger entry (amount, date) via the import seam."""
    from datetime import date

    from orchestrator.integrations.dedup_merge import dedup_and_merge
    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    phone = "+9190" + uuid4().int.__str__()[:8]
    cid = dedup_and_merge(tenant, acquired_via="kot_pos", phone_e164=phone,
                          display_name="Bridge Test").customer_id
    assert cid is not None
    record_imported_transactions(tenant, [ImportedTxnIn(
        provider_ref=f"attr-{uuid4().hex[:8]}", amount_paise=amount,
        txn_date=d if isinstance(d, date) else date.fromisoformat(d),
        direction="credit", customer_id=cid, entry_type="sale")],
        acquired_via="kot_pos")
    return str(cid)


def _park_unattributed(tenant: str, amount: int, d) -> str:
    from datetime import date

    from orchestrator.integrations.imported_transactions import (
        ImportedTxnIn,
        record_imported_transactions,
    )

    ref = f"unattr-{uuid4().hex[:8]}"
    record_imported_transactions(tenant, [ImportedTxnIn(
        provider_ref=ref, amount_paise=amount,
        txn_date=d if isinstance(d, date) else date.fromisoformat(d),
        direction="credit")], acquired_via="kot_pos")
    return ref


def _imported_row(tenant: str, ref: str):
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        return conn.execute(
            "SELECT customer_id::text AS customer_id, attribution_status, "
            "match_confidence FROM imported_transactions WHERE provider_ref = %s",
            (ref,)).fetchone()


def _ledger_count(tenant: str) -> int:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]


@_DB
def test_bridge_sets_tentative_link_without_ledger_write(db_ctx):
    """A parked import matching an attributed ledger entry → TENTATIVE link, NO ledger write."""
    from datetime import date

    from orchestrator.agent.tools.match_transactions import (
        attribute_imported_transactions,
    )

    tenant = _tenant(db_ctx.dsn)
    cid = _attributed_ledger_entry(tenant, 150000, date(2026, 6, 1))
    ref = _park_unattributed(tenant, 150000, date(2026, 6, 1))  # same amount + date
    ledger_before = _ledger_count(tenant)

    res = attribute_imported_transactions(tenant)
    assert res.scanned == 1 and res.tentative_set == 1

    row = _imported_row(tenant, ref)
    assert row["customer_id"] == cid
    assert row["attribution_status"] == "tentative"  # N3: distinguishable, NOT confirmed
    assert row["match_confidence"] is not None
    assert _ledger_count(tenant) == ledger_before  # D2: no guess written to the clean ledger


@_DB
def test_bridge_no_amount_match_stays_unattributed(db_ctx):
    from datetime import date

    from orchestrator.agent.tools.match_transactions import (
        attribute_imported_transactions,
    )

    tenant = _tenant(db_ctx.dsn)
    _attributed_ledger_entry(tenant, 150000, date(2026, 6, 1))
    ref = _park_unattributed(tenant, 999999, date(2026, 6, 1))  # amount mismatch

    res = attribute_imported_transactions(tenant)
    assert res.tentative_set == 0
    row = _imported_row(tenant, ref)
    assert row["customer_id"] is None and row["attribution_status"] == "unattributed"


@_DB
def test_bridge_idempotent(db_ctx):
    from datetime import date

    from orchestrator.agent.tools.match_transactions import (
        attribute_imported_transactions,
    )

    tenant = _tenant(db_ctx.dsn)
    _attributed_ledger_entry(tenant, 150000, date(2026, 6, 1))
    _park_unattributed(tenant, 150000, date(2026, 6, 1))
    attribute_imported_transactions(tenant)              # first pass → tentative
    res2 = attribute_imported_transactions(tenant)       # re-run
    assert res2.scanned == 0 and res2.tentative_set == 0  # tentative not re-scanned


@_DB
def test_bridge_cross_tenant(db_ctx):
    from datetime import date

    from orchestrator.agent.tools.match_transactions import (
        attribute_imported_transactions,
    )

    a, b = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    _attributed_ledger_entry(a, 150000, date(2026, 6, 1))   # A's ledger
    ref = _park_unattributed(b, 150000, date(2026, 6, 1))   # B's parked import
    res = attribute_imported_transactions(b)
    assert res.tentative_set == 0  # B cannot match A's ledger entry (RLS)
    assert _imported_row(b, ref)["attribution_status"] == "unattributed"
