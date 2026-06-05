"""VT-170 — customer_registry + redactor name_registry wiring tests.

CI stdlib-only smoke can run these (no langchain import in the privacy package).
VT-306: the registry now reads through CustomersWrapper.list_display_names (which
owns its tenant_connection + casefolds), so these patch that wrapper method
instead of injecting a mock pool. ``pool`` is still passed (vestigial) but unused.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

# Dep-less CI 'test' job: the patches target customer_registry -> db.wrappers ->
# tenant_connection -> psycopg. Skip cleanly when psycopg is absent; the real-PG suite runs it.
pytest.importorskip("psycopg")


@contextmanager
def _names(result: Any = None, *, raise_undefined: bool = False):
    """Patch CustomersWrapper.list_display_names to return ``result`` (a set of
    already-casefolded names) or raise an UndefinedTable-named exception."""
    def _fn(self: Any, tenant_id: Any, *, conn: Any = None) -> set[str]:
        if raise_undefined:
            raise type("UndefinedTable", (Exception,), {})("no relation")
        return set(result or set())

    with patch(
        "orchestrator.privacy.customer_registry.CustomersWrapper.list_display_names",
        _fn,
    ):
        yield


def setup_function() -> None:
    from orchestrator.privacy import customer_registry

    customer_registry.invalidate_all()


def test_names_fetched_and_casefolded() -> None:
    from orchestrator.privacy.customer_registry import get_customer_names_for_tenant

    with _names({"ravi kumar", "priya"}):
        names = get_customer_names_for_tenant("t1")
    assert "ravi kumar" in names
    assert "priya" in names


def test_cache_hit_skips_second_query() -> None:
    from orchestrator.privacy.customer_registry import get_customer_names_for_tenant

    with _names({"ravi"}):
        get_customer_names_for_tenant("t1")
    # Second call must be served from cache — even though the wrapper would now
    # return empty, the cached set is returned.
    with _names(set()):
        names = get_customer_names_for_tenant("t1")
    assert "ravi" in names


def test_invalidate_forces_refetch() -> None:
    from orchestrator.privacy import customer_registry
    from orchestrator.privacy.customer_registry import get_customer_names_for_tenant

    with _names({"old"}):
        get_customer_names_for_tenant("t1")
    customer_registry.invalidate("t1")
    with _names({"new"}):
        names = get_customer_names_for_tenant("t1")
    assert "new" in names
    assert "old" not in names


def test_undefined_table_returns_empty() -> None:
    from orchestrator.privacy.customer_registry import get_customer_names_for_tenant

    with _names(raise_undefined=True):
        names = get_customer_names_for_tenant("t1")
    assert names == frozenset()


def test_make_name_registry_predicate() -> None:
    from orchestrator.privacy.customer_registry import make_name_registry

    with _names({"ravi kumar"}):
        reg = make_name_registry("t1")
    assert reg("ravi kumar") is True
    assert reg("RAVI KUMAR") is True
    assert reg("someone else") is False


def test_redactor_uses_registry_none_safe() -> None:
    from orchestrator.privacy.pii_redactor import redact

    out_none = redact({"customer_name": "Ravi Kumar"})
    assert isinstance(out_none, dict)


def test_redactor_redacts_known_name_with_registry() -> None:
    from orchestrator.privacy.customer_registry import make_name_registry
    from orchestrator.privacy.pii_redactor import redact

    with _names({"ravi kumar"}):
        reg = make_name_registry("t1")
    out = redact({"note": "Ravi Kumar called"}, name_registry=reg)
    assert "Ravi Kumar" not in str(out)
