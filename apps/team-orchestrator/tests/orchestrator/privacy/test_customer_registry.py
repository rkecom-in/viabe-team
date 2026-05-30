"""VT-170 — customer_registry + redactor name_registry wiring tests.

CI stdlib-only smoke can run these (no langchain import in the privacy
package). Mock pool; no DB needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock



def _pool(rows: list[Any], *, raise_undefined: bool = False) -> Any:
    cur = MagicMock()

    def _execute(sql: str, params: tuple | None = None) -> None:
        if raise_undefined and "FROM customers" in sql:
            raise type("UndefinedTable", (Exception,), {})("no relation")

    cur.execute.side_effect = _execute
    cur.fetchall.return_value = rows
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def setup_function() -> None:
    from orchestrator.privacy import customer_registry

    customer_registry.invalidate_all()


def test_names_fetched_and_casefolded() -> None:
    from orchestrator.privacy.customer_registry import (
        get_customer_names_for_tenant,
    )
    pool = _pool([{"display_name": "Ravi Kumar"}, {"display_name": "PRIYA"}])
    names = get_customer_names_for_tenant("t1", pool=pool)
    assert "ravi kumar" in names
    assert "priya" in names


def test_cache_hit_skips_second_query() -> None:
    from orchestrator.privacy.customer_registry import (
        get_customer_names_for_tenant,
    )
    pool = _pool([{"display_name": "Ravi"}])
    get_customer_names_for_tenant("t1", pool=pool)
    # Second call served from cache — a fresh (empty) pool would yield
    # empty if it queried; cache must return the original set.
    empty_pool = _pool([])
    names = get_customer_names_for_tenant("t1", pool=empty_pool)
    assert "ravi" in names
    empty_pool.connection.assert_not_called()


def test_invalidate_forces_refetch() -> None:
    from orchestrator.privacy import customer_registry
    from orchestrator.privacy.customer_registry import (
        get_customer_names_for_tenant,
    )
    get_customer_names_for_tenant("t1", pool=_pool([{"display_name": "Old"}]))
    customer_registry.invalidate("t1")
    names = get_customer_names_for_tenant(
        "t1", pool=_pool([{"display_name": "New"}])
    )
    assert "new" in names
    assert "old" not in names


def test_undefined_table_returns_empty() -> None:
    from orchestrator.privacy.customer_registry import (
        get_customer_names_for_tenant,
    )
    names = get_customer_names_for_tenant(
        "t1", pool=_pool([], raise_undefined=True)
    )
    assert names == frozenset()


def test_make_name_registry_predicate() -> None:
    from orchestrator.privacy.customer_registry import make_name_registry

    reg = make_name_registry("t1", pool=_pool([{"display_name": "Ravi Kumar"}]))
    assert reg("ravi kumar") is True
    assert reg("RAVI KUMAR") is True
    assert reg("someone else") is False


def test_redactor_uses_registry_none_safe() -> None:
    # Import the canonical redactor directly (smoke-safe — the
    # observability.pii wrapper pulls a heavy observability/__init__ chain
    # not present in the stdlib-only smoke job; the wrapper just forwards
    # name_registry to this same function).
    from orchestrator.privacy.pii_redactor import redact

    # None-safe: no registry → does not crash, returns a dict.
    out_none = redact({"customer_name": "Ravi Kumar"})
    assert isinstance(out_none, dict)


def test_redactor_redacts_known_name_with_registry() -> None:
    from orchestrator.privacy.customer_registry import make_name_registry
    from orchestrator.privacy.pii_redactor import redact

    reg = make_name_registry("t1", pool=_pool([{"display_name": "Ravi Kumar"}]))
    out = redact({"note": "Ravi Kumar called"}, name_registry=reg)
    # The known name should not survive verbatim in a free-text field.
    assert "Ravi Kumar" not in str(out)
