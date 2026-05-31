"""VT-41 — get_business_profile tests.

CI smoke (`--no-project`) skips this file via importorskip("langchain")
because `orchestrator.agent.__init__` pulls in langchain.agents.
Full coverage runs in the `migrations` job (uv sync --frozen).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain")


def _undefined_table_exc() -> Exception:
    return type("UndefinedTable", (Exception,), {})("relation does not exist")


def _fake_pool(
    *,
    tenant_row: Any,
    connector_rows: list[Any] | None = None,
    l1_row: Any | None = None,
    connector_table_missing: bool = False,
    l1_table_missing: bool = False,
) -> Any:
    """Stub psycopg pool. Tracks execute call index so multiple SELECTs
    return the right fixture row in order:
    [0] set_config, [1] SELECT tenants, [2] SELECT tenant_connector_status,
    [3] SELECT l1_entities (VT-195: business_profile entity).
    """
    cur = MagicMock()

    fetchone_q: list[Any] = [tenant_row, l1_row]
    fetchall_q: list[list[Any]] = [connector_rows or []]

    execute_calls: list[int] = [0]

    def _execute(sql: str, _p: tuple | None = None) -> None:
        execute_calls[0] += 1
        if connector_table_missing and "tenant_connector_status" in sql:
            raise _undefined_table_exc()
        if l1_table_missing and "l1_entities" in sql:
            raise _undefined_table_exc()

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: (
        fetchone_q.pop(0) if fetchone_q else None
    )
    cur.fetchall.side_effect = lambda: (
        fetchall_q.pop(0) if fetchall_q else []
    )
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def test_pydantic_io_shape() -> None:
    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        GetBusinessProfileOutput,
    )
    inp = GetBusinessProfileInput(tenant_id="t1")
    assert inp.tenant_id == "t1"

    out = GetBusinessProfileOutput(
        business_name="Acme Tiffin",
        business_archetype="tiffin_service",
        owner_name=None,
        locale="en-IN",
        working_hours=None,
        integration_summary=["google_drive", "razorpay"],
        owner_curated_context=None,
    )
    assert out.business_name == "Acme Tiffin"
    assert out.integration_summary == ["google_drive", "razorpay"]


def test_returns_none_when_tenant_missing() -> None:
    if os.environ.get("VT41_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        get_business_profile,
    )
    pool = _fake_pool(tenant_row=None)
    result = get_business_profile(
        GetBusinessProfileInput(tenant_id="nope"),
        pool=pool,
    )
    assert result is None


def test_returns_profile_with_integrations_and_l1() -> None:
    if os.environ.get("VT41_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        get_business_profile,
    )
    pool = _fake_pool(
        tenant_row={
            "business_name": "Acme Tiffin",
            "business_type": "tiffin_service",
            "preferred_language": None,
            "language_preference": "hi",
        },
        connector_rows=[
            {"connector_id": "google_drive"},
            {"connector_id": "razorpay"},
        ],
        l1_row={"owner_curated_context": "Owner cares about veg orders."},
    )
    result = get_business_profile(
        GetBusinessProfileInput(tenant_id="t1"),
        pool=pool,
    )
    assert result is not None
    assert result.business_name == "Acme Tiffin"
    assert result.business_archetype == "tiffin_service"
    assert result.locale == "hi"
    assert result.integration_summary == ["google_drive", "razorpay"]
    assert result.owner_curated_context == "Owner cares about veg orders."


def test_l1_absent_returns_null_gracefully() -> None:
    if os.environ.get("VT41_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        get_business_profile,
    )
    pool = _fake_pool(
        tenant_row={
            "business_name": "Acme",
            "business_type": "retail",
            "preferred_language": "en",
            "language_preference": "en",
        },
        connector_rows=[],
        l1_table_missing=True,
    )
    result = get_business_profile(
        GetBusinessProfileInput(tenant_id="t1"),
        pool=pool,
    )
    assert result is not None
    assert result.owner_curated_context is None
    assert result.integration_summary == []


def test_connector_table_missing_returns_empty_integrations() -> None:
    if os.environ.get("VT41_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        get_business_profile,
    )
    pool = _fake_pool(
        tenant_row={
            "business_name": "Acme",
            "business_type": "retail",
            "preferred_language": "en",
            "language_preference": "en",
        },
        connector_table_missing=True,
        l1_table_missing=True,
    )
    result = get_business_profile(
        GetBusinessProfileInput(tenant_id="t1"),
        pool=pool,
    )
    assert result is not None
    assert result.integration_summary == []
