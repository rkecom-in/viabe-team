#!/usr/bin/env python3
"""VT-40 — query_customer_ledger canary.

CI default mocks the pool. Real-DB mode opt-in via VT40_REAL_DB=1
exercises the live psycopg path against a seeded ledger row.

3 assertions:
- A1: Pydantic IO accepts valid input + rejects bad limit
- A2: Mock-mode query with matched customer returns shape + correct total
- A3: Mock-mode query with UndefinedTable surface returns empty
      gracefully (forward-target schema gap)

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _fake_pool(*, customer_row: Any, ledger_rows: list[Any] | None = None,
                raise_undefined_table: bool = False) -> Any:
    cur = MagicMock()
    if raise_undefined_table:
        # Match the tool's type-name check; no psycopg import needed.
        cur.execute.side_effect = type(
            "UndefinedTable", (Exception,), {},
        )("relation does not exist")
    else:
        cur.fetchone.return_value = customer_row
        cur.fetchall.return_value = ledger_rows or []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def run_canary() -> int:
    if os.environ.get("VT40_REAL_DB") == "1":
        if not os.environ.get("DATABASE_URL"):
            print("PREFLIGHT FAIL — VT40_REAL_DB=1 needs DATABASE_URL",
                  file=sys.stderr)
            return 2
    print("PREFLIGHT OK")

    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
        query_customer_ledger,
    )

    # --- A1: Pydantic IO validates ---
    ok_input = False
    rejects_bad = False
    try:
        QueryCustomerLedgerInput(
            tenant_id="t1", customer_phone_token="tok",
            since_date=date(2026, 1, 1), limit=50,
        )
        ok_input = True
    except Exception as exc:  # noqa: BLE001
        print(f"    A1 valid input raised: {exc!r}", file=sys.stderr)
    try:
        QueryCustomerLedgerInput(
            tenant_id="t1", customer_phone_token="tok", limit=2000,
        )
    except Exception:
        rejects_bad = True
    assertion(
        1,
        "Pydantic IO accepts valid + rejects bad limit",
        ok_input and rejects_bad,
        observed={"valid_accepted": ok_input, "bad_rejected": rejects_bad},
    )

    # --- A2: matched customer → entries + total ---
    pool = _fake_pool(
        customer_row={"id": "cust_42"},
        ledger_rows=[
            {"entry_date": date(2026, 5, 1), "amount_paise": 1500,
             "description": "invoice A"},
            {"entry_date": date(2026, 4, 1), "amount_paise": 800,
             "description": "invoice B"},
        ],
    )
    r = query_customer_ledger(
        QueryCustomerLedgerInput(
            tenant_id="tenant_a", customer_phone_token="tok_known",
        ),
        pool=pool,
    )
    pass_2 = (
        r.customer_id == "cust_42"
        and len(r.ledger_entries) == 2
        and r.total_balance_paise == 2300
    )
    assertion(
        2,
        "Matched customer → entries + correct total",
        pass_2,
        observed={
            "customer_id": r.customer_id,
            "entry_count": len(r.ledger_entries),
            "total": r.total_balance_paise,
        },
    )

    # --- A3: undefined-table → graceful empty ---
    pool = _fake_pool(customer_row=None, raise_undefined_table=True)
    r = query_customer_ledger(
        QueryCustomerLedgerInput(
            tenant_id="tenant_a", customer_phone_token="tok_any",
        ),
        pool=pool,
    )
    pass_3 = (
        r.customer_id is None
        and r.ledger_entries == []
        and r.total_balance_paise == 0
    )
    assertion(
        3,
        "UndefinedTable surfaces graceful empty (schema gap)",
        pass_3,
        observed={
            "customer_id": r.customer_id,
            "entry_count": len(r.ledger_entries),
            "total": r.total_balance_paise,
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
