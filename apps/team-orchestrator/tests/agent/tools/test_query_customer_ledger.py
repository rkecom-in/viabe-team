"""VT-40 — query_customer_ledger tests.

CI default mocks the connection pool. Real-mode opt-in via
`VT40_REAL_DB=1` exercises the live psycopg path (release-prep
manual only; never fires in CI per VT-32 hard rule).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

# Importing the tool triggers `orchestrator.agent.__init__`, which pulls
# in `langchain.agents`. CI's stdlib-only `test` job (`uv run --no-project
# --with pytest pytest`) doesn't install langchain — skip the whole file
# in that environment. Full coverage runs in the `migrations` job
# (uv sync --frozen + heavy deps).
pytest.importorskip("langchain")


def _fake_pool(*, customer_row: Any, ledger_rows: list[Any] | None = None,
                raise_undefined_table: bool = False) -> Any:
    """Minimal psycopg pool stub. Two sequential cursor.execute calls:
    set_config + customer SELECT + ledger SELECT (fetchone + fetchall).
    """
    cur = MagicMock()
    if raise_undefined_table:
        # Class name must be 'UndefinedTable' — tool matches on type
        # name to stay psycopg-free at module load.
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


def test_pydantic_io_shape_validates() -> None:
    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
        QueryCustomerLedgerOutput,
        LedgerEntry,
    )
    inp = QueryCustomerLedgerInput(
        tenant_id="t1", customer_phone_token="tok_abc",
        since_date=date(2026, 1, 1), limit=50,
    )
    assert inp.tenant_id == "t1"
    assert inp.limit == 50

    out = QueryCustomerLedgerOutput(
        customer_id="cust_1",
        ledger_entries=[
            LedgerEntry(entry_date=date(2026, 5, 1), amount_paise=1500,
                        description="invoice"),
        ],
        total_balance_paise=1500,
    )
    assert out.customer_id == "cust_1"
    assert len(out.ledger_entries) == 1


def test_query_returns_empty_when_no_customer_match() -> None:
    if os.environ.get("VT40_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
        query_customer_ledger,
    )
    pool = _fake_pool(customer_row=None)
    result = query_customer_ledger(
        QueryCustomerLedgerInput(
            tenant_id="tenant_a", customer_phone_token="tok_missing",
        ),
        pool=pool,
    )
    assert result.customer_id is None
    assert result.ledger_entries == []
    assert result.total_balance_paise == 0


def test_query_returns_entries_when_match() -> None:
    if os.environ.get("VT40_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
        query_customer_ledger,
    )
    pool = _fake_pool(
        customer_row={"id": "cust_42"},
        ledger_rows=[
            {"entry_date": date(2026, 5, 1), "amount_paise": 1500,
             "description": "invoice A"},
            {"entry_date": date(2026, 4, 1), "amount_paise": 800,
             "description": "invoice B"},
        ],
    )
    result = query_customer_ledger(
        QueryCustomerLedgerInput(
            tenant_id="tenant_a", customer_phone_token="tok_known",
        ),
        pool=pool,
    )
    assert result.customer_id == "cust_42"
    assert len(result.ledger_entries) == 2
    assert result.total_balance_paise == 2300


def test_query_returns_empty_on_undefined_table() -> None:
    """Forward-target schema not landed yet — graceful empty."""
    if os.environ.get("VT40_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
        query_customer_ledger,
    )
    pool = _fake_pool(customer_row=None, raise_undefined_table=True)
    result = query_customer_ledger(
        QueryCustomerLedgerInput(
            tenant_id="tenant_a", customer_phone_token="tok_any",
        ),
        pool=pool,
    )
    assert result.customer_id is None
    assert result.ledger_entries == []
    assert result.total_balance_paise == 0


def test_input_rejects_invalid_limit() -> None:
    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
    )
    with pytest.raises(ValueError):
        QueryCustomerLedgerInput(
            tenant_id="t1", customer_phone_token="tok", limit=0,
        )
    with pytest.raises(ValueError):
        QueryCustomerLedgerInput(
            tenant_id="t1", customer_phone_token="tok", limit=2000,
        )
