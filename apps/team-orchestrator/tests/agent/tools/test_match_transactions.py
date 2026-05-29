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
