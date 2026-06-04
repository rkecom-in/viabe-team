"""VT-170 — resolve_cohort_recipients tests.

VT-306 (bounce fix): the standalone path now opens a ``tenant_connection``
(SET ROLE app_role + GUC) instead of a raw pool.connection()+set_config, and the
customers validate goes through CustomersWrapper.filter_existing_ids. These tests
patch ``tenant_connection`` to yield a mock conn (no live DB) that serves the
validate SELECT + records the campaign_recipients INSERTs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

pytest.importorskip("pydantic")

# A real UUID tenant — the wrapper UUID-validates tenant_id.
_T = "00000000-0000-4000-8000-000000000001"


@contextmanager
def _patch_tc(monkeypatch: Any, *, real_ids: list[str]):
    """Patch orchestrator.db.tenant_connection to yield a mock conn. The conn's
    customers SELECT returns ``real_ids`` (as {id, tenant_id} rows for the
    wrapper's _validate); current_user reads as a mock (hardening skips it);
    INSERTs are captured. Yields the captured calls list."""
    calls: list[tuple[str, tuple]] = []
    cur = MagicMock()

    def _execute(sql: str, params: tuple | None = None) -> Any:
        calls.append((sql, params or ()))
        result = MagicMock()
        if "FROM customers" in sql:
            result.fetchall.return_value = [
                {"id": cid, "tenant_id": UUID(_T)} for cid in real_ids
            ]
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = MagicMock()  # current_user etc.
        return result

    cur.execute.side_effect = _execute

    @contextmanager
    def _fake_tc(tenant_id: Any):
        calls.append(("__tenant_connection__", (str(tenant_id),)))
        yield cur

    monkeypatch.setattr("orchestrator.db.tenant_connection", _fake_tc)
    yield calls


def test_all_resolved(monkeypatch) -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    with _patch_tc(monkeypatch, real_ids=["c1", "c2"]) as calls:
        out = resolve_cohort_recipients(
            tenant_id=_T, campaign_id="camp1", customer_ids=["c1", "c2"], pool=object(),
        )
    assert sorted(out.resolved) == ["c1", "c2"]
    assert out.rejected == []
    assert len([c for c in calls if "INSERT INTO campaign_recipients" in c[0]]) == 2


def test_unknown_id_rejected_not_dropped(monkeypatch) -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    with _patch_tc(monkeypatch, real_ids=["c1"]) as calls:
        out = resolve_cohort_recipients(
            tenant_id=_T, campaign_id="camp1", customer_ids=["c1", "c2"], pool=object(),
        )
    assert out.resolved == ["c1"]
    assert out.rejected == ["c2"]
    assert set(out.resolved) | set(out.rejected) == {"c1", "c2"}
    assert len([c for c in calls if "INSERT INTO campaign_recipients" in c[0]]) == 1


def test_dedupes_and_orders(monkeypatch) -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    with _patch_tc(monkeypatch, real_ids=["a", "b"]):
        out = resolve_cohort_recipients(
            tenant_id=_T, campaign_id="camp1",
            customer_ids=["b", "a", "b", "a"], pool=object(),
        )
    assert out.resolved == ["a", "b"]


def test_empty_cohort_noop(monkeypatch) -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    with _patch_tc(monkeypatch, real_ids=[]) as calls:
        out = resolve_cohort_recipients(
            tenant_id=_T, campaign_id="camp1", customer_ids=[], pool=object(),
        )
    assert out.resolved == []
    assert out.rejected == []
    # Empty cohort short-circuits before opening any connection.
    assert calls == []


def test_opens_tenant_connection_for_scope(monkeypatch) -> None:
    """VT-306: scope is enforced by tenant_connection (SET ROLE app_role + GUC),
    opened for the resolving tenant — not a raw pool+set_config."""
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    with _patch_tc(monkeypatch, real_ids=["c1"]) as calls:
        resolve_cohort_recipients(
            tenant_id=_T, campaign_id="camp1", customer_ids=["c1"], pool=object(),
        )
    tc_calls = [c for c in calls if c[0] == "__tenant_connection__"]
    assert tc_calls and tc_calls[0][1] == (_T,)
