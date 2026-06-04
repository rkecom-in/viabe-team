"""VT-170 — resolve_cohort_recipients tests (mock pool)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# cohort.py imports pydantic; CI's stdlib-only smoke job (uv run
# --no-project --with pytest) doesn't install it. Full coverage runs in
# the orchestrator job (uv sync --frozen).
pytest.importorskip("pydantic")

# VT-306: cohort's customers validate now goes through CustomersWrapper, which
# UUID-validates tenant_id (a good tightening). The mock-cur tests therefore use a
# real UUID tenant; customer_ids stay opaque (the mock cur ignores the ANY bind).
_T = "00000000-0000-4000-8000-000000000001"


def _pool(*, real_ids: list[str]) -> tuple[Any, list[tuple[str, tuple]]]:
    """Stub: set_config, SELECT real customer ids (fetchall), N inserts."""
    calls: list[tuple[str, tuple]] = []
    cur = MagicMock()

    def _execute(sql: str, params: tuple | None = None) -> Any:
        calls.append((sql, params or ()))
        return cur  # real psycopg returns the cursor → wrapper chains .fetchall()

    cur.execute.side_effect = _execute
    cur.fetchall.return_value = [{"id": cid} for cid in real_ids]
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, calls


def test_all_resolved() -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    pool, calls = _pool(real_ids=["c1", "c2"])
    out = resolve_cohort_recipients(
        tenant_id=_T, campaign_id="camp1",
        customer_ids=["c1", "c2"], pool=pool,
    )
    assert sorted(out.resolved) == ["c1", "c2"]
    assert out.rejected == []
    inserts = [c for c in calls if "INSERT INTO campaign_recipients" in c[0]]
    assert len(inserts) == 2


def test_unknown_id_rejected_not_dropped() -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    # c2 is NOT a real customer → must surface in rejected, never linked.
    pool, calls = _pool(real_ids=["c1"])
    out = resolve_cohort_recipients(
        tenant_id=_T, campaign_id="camp1",
        customer_ids=["c1", "c2"], pool=pool,
    )
    assert out.resolved == ["c1"]
    assert out.rejected == ["c2"]
    # Every input id accounted for (no silent drop).
    assert set(out.resolved) | set(out.rejected) == {"c1", "c2"}
    inserts = [c for c in calls if "INSERT INTO campaign_recipients" in c[0]]
    assert len(inserts) == 1  # only the resolved id inserted


def test_dedupes_and_orders() -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    pool, _ = _pool(real_ids=["a", "b"])
    out = resolve_cohort_recipients(
        tenant_id=_T, campaign_id="camp1",
        customer_ids=["b", "a", "b", "a"], pool=pool,
    )
    # Deterministic sorted-unique output (reproducible).
    assert out.resolved == ["a", "b"]


def test_empty_cohort_noop() -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    pool, calls = _pool(real_ids=[])
    out = resolve_cohort_recipients(
        tenant_id=_T, campaign_id="camp1", customer_ids=[], pool=pool,
    )
    assert out.resolved == []
    assert out.rejected == []
    # No DB round-trip for an empty cohort.
    pool.connection.assert_not_called()


def test_sets_tenant_guc_first() -> None:
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    pool, calls = _pool(real_ids=["c1"])
    resolve_cohort_recipients(
        tenant_id=_T, campaign_id="camp1",
        customer_ids=["c1"], pool=pool,
    )
    assert "set_config('app.current_tenant'" in calls[0][0]
