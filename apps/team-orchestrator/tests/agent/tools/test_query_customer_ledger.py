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


def _typed_exc(type_name: str, message: str) -> Exception:
    """Build an exception whose class NAME matches what the tool checks
    (psycopg-free; the tool matches on type(exc).__name__ + message)."""
    return type(type_name, (Exception,), {})(message)


def _fake_pool(*, customer_row: Any, ledger_rows: list[Any] | None = None,
                raise_undefined_table: bool = False,
                raise_exc: Exception | None = None) -> Any:
    """Minimal psycopg pool stub. Two sequential cursor.execute calls:
    set_config + customer SELECT + ledger SELECT (fetchone + fetchall).
    """
    cur = MagicMock()
    if raise_exc is not None:
        cur.execute.side_effect = raise_exc
    elif raise_undefined_table:
        # The real forward-target: customer_ledger_entries is absent. Message
        # must carry the table name (VT-264 narrowing matches on it).
        cur.execute.side_effect = _typed_exc(
            "UndefinedTable",
            'relation "customer_ledger_entries" does not exist',
        )
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


def _input_any():
    from orchestrator.agent.tools.query_customer_ledger import QueryCustomerLedgerInput

    return QueryCustomerLedgerInput(tenant_id="tenant_a", customer_phone_token="tok")


def test_known_phone_token_undefined_column_returns_empty() -> None:
    """VT-264: the SPECIFIC forward-absent customers.phone_token → graceful empty."""
    if os.environ.get("VT40_REAL_DB") == "1":
        pytest.skip("real-DB mode active")
    from orchestrator.agent.tools.query_customer_ledger import query_customer_ledger

    pool = _fake_pool(
        customer_row=None,
        raise_exc=_typed_exc("UndefinedColumn", 'column "phone_token" does not exist'),
    )
    result = query_customer_ledger(_input_any(), pool=pool)
    assert result.customer_id is None and result.ledger_entries == []


def test_foreign_undefined_column_RAISES() -> None:
    """VT-264 narrowing: an UndefinedColumn that is NOT phone_token must RAISE —
    a real query typo/schema-drift bug surfaces, not a silent empty result."""
    if os.environ.get("VT40_REAL_DB") == "1":
        pytest.skip("real-DB mode active")
    from orchestrator.agent.tools.query_customer_ledger import query_customer_ledger

    pool = _fake_pool(
        customer_row=None,
        raise_exc=_typed_exc("UndefinedColumn", 'column "totally_bogus_col" does not exist'),
    )
    with pytest.raises(Exception, match="totally_bogus_col"):
        query_customer_ledger(_input_any(), pool=pool)


def test_foreign_undefined_table_RAISES() -> None:
    """VT-264 narrowing: an UndefinedTable that is NOT customer_ledger_entries RAISES."""
    if os.environ.get("VT40_REAL_DB") == "1":
        pytest.skip("real-DB mode active")
    from orchestrator.agent.tools.query_customer_ledger import query_customer_ledger

    pool = _fake_pool(
        customer_row=None,
        raise_exc=_typed_exc("UndefinedTable", 'relation "some_other_table" does not exist'),
    )
    with pytest.raises(Exception, match="some_other_table"):
        query_customer_ledger(_input_any(), pool=pool)


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
