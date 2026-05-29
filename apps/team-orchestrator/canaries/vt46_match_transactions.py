#!/usr/bin/env python3
"""VT-46 — match_transactions canary.

4 assertions (mock-mode CI; real-mode VT46_REAL_DB=1):
- A1: Pydantic IO validates
- A2: Exact amount + time → match with composite > 0.5 + "amount+time"
- A3: Amount mismatch → unmatched with reason
- A4: VPA fuzzy match boosts confidence + picks correct ledger row

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
T0 = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _ledger(*, id: str, amount: int, ts: datetime,
             vpa: str | None = None) -> dict[str, Any]:
    return {"id": id, "amount_paise": amount, "entry_ts": ts, "ref_vpa": vpa}


def run_canary() -> int:
    if os.environ.get("VT46_REAL_DB") == "1":
        if not os.environ.get("DATABASE_URL"):
            print("PREFLIGHT FAIL — VT46_REAL_DB=1 needs DATABASE_URL",
                  file=sys.stderr)
            return 2
    print("PREFLIGHT OK")

    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        MatchTransactionsOutput,
        TransactionInput,
        match_transactions,
    )

    # --- A1: IO ---
    ok = False
    try:
        MatchTransactionsInput(
            tenant_id="t1",
            transactions=[
                TransactionInput(
                    txn_id="x", amount_paise=10, timestamp=T0,
                ),
            ],
        )
        MatchTransactionsOutput(matches=[], unmatched=[])
        ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"    A1 raised: {exc!r}", file=sys.stderr)
    assertion(1, "Pydantic IO validates", ok, observed={"ok": ok})

    # --- A2: amount + time match ---
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
            ),
        ],
    )
    r = match_transactions(
        payload, candidate_ledger=[
            _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=2)),
        ],
    )
    pass_2 = (
        len(r.matches) == 1
        and r.matches[0].ledger_entry_id == "L1"
        and r.matches[0].confidence > 0.5
        and "amount" in r.matches[0].match_basis
        and "time" in r.matches[0].match_basis
    )
    assertion(
        2,
        "Exact amount + close time → match (amount+time basis)",
        pass_2,
        observed={
            "match_count": len(r.matches),
            "confidence": r.matches[0].confidence if r.matches else None,
            "basis": r.matches[0].match_basis if r.matches else None,
        },
    )

    # --- A3: amount mismatch ---
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI002", amount_paise=20000, timestamp=T0,
            ),
        ],
    )
    r = match_transactions(
        payload, candidate_ledger=[
            _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=2)),
        ],
    )
    pass_3 = (
        r.matches == []
        and len(r.unmatched) == 1
        and r.unmatched[0].reason == "no_amount_match"
    )
    assertion(
        3,
        "Amount mismatch → unmatched (no_amount_match)",
        pass_3,
        observed={
            "matched": len(r.matches),
            "reason": r.unmatched[0].reason if r.unmatched else None,
        },
    )

    # --- A4: VPA boosts + picks right row ---
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(
                txn_id="UPI001", amount_paise=15000, timestamp=T0,
                vpa="customer.foo@upi",
            ),
        ],
    )
    r = match_transactions(
        payload, candidate_ledger=[
            _ledger(id="L1", amount=15000, ts=T0 + timedelta(hours=1),
                    vpa="customer.foo@upi"),
            _ledger(id="L2", amount=15000, ts=T0 + timedelta(hours=1),
                    vpa="someone.else@upi"),
        ],
    )
    pass_4 = (
        len(r.matches) == 1
        and r.matches[0].ledger_entry_id == "L1"
        and "vpa" in r.matches[0].match_basis
    )
    assertion(
        4,
        "VPA fuzzy match picks correct ledger row",
        pass_4,
        observed={
            "matched_id": (
                r.matches[0].ledger_entry_id if r.matches else None
            ),
            "basis": r.matches[0].match_basis if r.matches else None,
        },
    )

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)",
              file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
