"""VT-43 — get_attribution_data tests.

CI stdlib-only smoke skips via importorskip("langchain").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain")

T0 = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _undefined_table_exc() -> Exception:
    return type("UndefinedTable", (Exception,), {})("relation does not exist")


# VT-306: the campaign read goes through CampaignsWrapper (UUID-validates tenant).
_T = "00000000-0000-4000-8000-000000000abc"

# VT-306 bounce: _campaign_mode/_window_mode now read via the wrapper on its OWN
# tenant_connection (NOT the surrounding pool+set_config cur). Patch the wrapper
# methods to return the staged rows; the cur serves only the (non-hot) attributions
# aggregate.
_CAMPAIGN_ROW: list[Any] = [None]
_WINDOW_ROWS: list[Any] = [None]
# VT-306 bounce-2: get_attribution_data's OUTER conn is now a tenant_connection
# (for the RLS-enforced attributions read). Patch it to yield the mock cur.
_TC_CONN: list[Any] = [None]
_TC_TENANTS: list[str] = []


@pytest.fixture(autouse=True)
def _patch_campaign_wrapper(monkeypatch):
    from contextlib import contextmanager

    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(
        CampaignsWrapper, "find_by_id",
        lambda self, tenant_id, row_id, **kw: _CAMPAIGN_ROW[0],
    )
    monkeypatch.setattr(
        CampaignsWrapper, "attribution_window_summary",
        lambda self, tenant_id, ws, we, **kw: list(_WINDOW_ROWS[0] or []),
    )

    @contextmanager
    def _fake_tc(tenant_id):
        _TC_TENANTS.append(str(tenant_id))
        yield _TC_CONN[0]

    monkeypatch.setattr("orchestrator.db.tenant_connection", _fake_tc)
    _CAMPAIGN_ROW[0] = None
    _WINDOW_ROWS[0] = None
    _TC_CONN[0] = None
    _TC_TENANTS.clear()
    yield


def _pool_campaign(*, campaign_row: Any, agg_row: Any,
                    raise_undefined: bool = False) -> tuple[Any, list[str]]:
    """Stub for campaign mode: SET LOCAL, SELECT campaign (fetchone),
    SELECT aggregate (fetchone)."""
    issued_sql: list[str] = []
    cur = MagicMock()
    _CAMPAIGN_ROW[0] = campaign_row  # served by the patched find_by_id
    _TC_CONN[0] = cur  # outer tenant_connection yields this cur (attributions read)
    fetchone_q = [agg_row]  # cur serves ONLY the (non-hot) attributions agg

    def _execute(sql: str, params: tuple | None = None) -> Any:
        issued_sql.append(sql)
        if raise_undefined and "FROM attributions" in sql:
            raise _undefined_table_exc()
        return cur  # real psycopg returns the cursor → wrapper chains .fetchone()

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: fetchone_q.pop(0) if fetchone_q else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, issued_sql


def _pool_window(*, rows: list[Any]) -> Any:
    _WINDOW_ROWS[0] = rows  # served by the patched attribution_window_summary
    cur = MagicMock()
    _TC_CONN[0] = cur  # outer tenant_connection yields this cur (window mode: unused)
    cur.execute.side_effect = lambda sql, params=None: cur
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


def test_xor_validation() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
    )
    # both → error
    with pytest.raises(ValueError):
        GetAttributionDataInput(tenant_id=_T, campaign_id="c", window_start=T0)
    # neither → error
    with pytest.raises(ValueError):
        GetAttributionDataInput(tenant_id=_T)
    # window with only one bound → error
    with pytest.raises(ValueError):
        GetAttributionDataInput(tenant_id=_T, window_start=T0)
    # reversed window → error
    with pytest.raises(ValueError):
        GetAttributionDataInput(tenant_id=_T, window_start=T1, window_end=T0)
    # valid campaign
    GetAttributionDataInput(tenant_id=_T, campaign_id="c")
    # valid window
    GetAttributionDataInput(tenant_id=_T, window_start=T0, window_end=T1)


def test_campaign_mode_closed_with_attributions() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    pool, _ = _pool_campaign(
        campaign_row={
            "tenant_id": UUID(_T),
            "attribution_close_at": T1,
            "attribution_closed_at": T1,
            "total_arrr_paise": 5000,
        },
        agg_row={"transacting_count": 3, "arrr_paise": 5000},
    )
    out = get_attribution_data(
        GetAttributionDataInput(tenant_id=_T, campaign_id="c1"), pool=pool
    )
    assert out.mode == "campaign"
    assert out.campaign is not None
    assert out.campaign.attribution_status == "closed"
    assert out.campaign.transacting_count == 3
    assert out.campaign.arrr_paise == 5000
    # Option A degraded fields are None (not 0) — honest.
    assert out.campaign.cohort_size is None
    assert out.campaign.attribution_rate is None
    assert out.complete is False


def test_campaign_mode_pending_emits_note() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    pool, _ = _pool_campaign(
        campaign_row={
            "tenant_id": UUID(_T),
            "attribution_close_at": T1,
            "attribution_closed_at": None,
            "total_arrr_paise": None,
        },
        agg_row={"transacting_count": 0, "arrr_paise": 0},
    )
    out = get_attribution_data(
        GetAttributionDataInput(tenant_id=_T, campaign_id="c1"), pool=pool
    )
    assert out.campaign is not None
    assert out.campaign.attribution_status == "pending"
    assert any("pending" in n for n in out.notes)


def test_campaign_mode_cached_mismatch_note() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    pool, _ = _pool_campaign(
        campaign_row={
            "tenant_id": UUID(_T),
            "attribution_close_at": T1,
            "attribution_closed_at": T1,
            "total_arrr_paise": 9999,  # cached differs from live SUM
        },
        agg_row={"transacting_count": 2, "arrr_paise": 4000},
    )
    out = get_attribution_data(
        GetAttributionDataInput(tenant_id=_T, campaign_id="c1"), pool=pool
    )
    assert out.campaign is not None
    assert out.campaign.arrr_paise == 4000  # live SUM wins
    assert any("cached" in n and "live SUM" in n for n in out.notes)


def test_window_mode_aggregates() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    pool = _pool_window(rows=[
        {"campaign_id": "a", "attribution_closed_at": T1,
         "transacting_count": 2, "arrr_paise": 3000},
        {"campaign_id": "b", "attribution_closed_at": None,
         "transacting_count": 1, "arrr_paise": 1500},
    ])
    out = get_attribution_data(
        GetAttributionDataInput(tenant_id=_T, window_start=T0, window_end=T1),
        pool=pool,
    )
    assert out.mode == "window"
    assert out.window is not None
    assert out.window.campaign_count == 2
    assert out.window.total_transacting_count == 3
    assert out.window.total_arrr_paise == 4500
    assert len(out.window.per_campaign_summary) == 2
    assert out.window.per_campaign_summary[0].campaign_id == "a"


def test_reproducibility_byte_identical() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    inp = GetAttributionDataInput(tenant_id=_T, campaign_id="c1")

    def _run() -> str:
        pool, _ = _pool_campaign(
            campaign_row={
            "tenant_id": UUID(_T),
                "attribution_close_at": T1,
                "attribution_closed_at": T1,
                "total_arrr_paise": 5000,
            },
            agg_row={"transacting_count": 3, "arrr_paise": 5000},
        )
        return get_attribution_data(inp, pool=pool).model_dump_json()

    assert _run() == _run()  # byte-identical


def test_sets_tenant_guc_before_query() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    pool, issued = _pool_campaign(
        campaign_row={
            "tenant_id": UUID(_T),
            "attribution_close_at": T1, "attribution_closed_at": T1,
            "total_arrr_paise": 100,
        },
        agg_row={"transacting_count": 1, "arrr_paise": 100},
    )
    get_attribution_data(
        GetAttributionDataInput(tenant_id=_T, campaign_id="c1"),
        pool=pool,
    )
    # VT-306 bounce-2: scope is enforced by opening a tenant_connection (SET ROLE
    # app_role + GUC) for the resolving tenant — NOT inline set_config on a raw
    # pool conn. Assert that's what the read runs under.
    assert _TC_TENANTS == [_T]


def test_undefined_table_graceful_empty() -> None:
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )
    pool, _ = _pool_campaign(
        campaign_row={
            "tenant_id": UUID(_T),
            "attribution_close_at": T1, "attribution_closed_at": T1,
            "total_arrr_paise": 0,
        },
        agg_row=None,
        raise_undefined=True,
    )
    out = get_attribution_data(
        GetAttributionDataInput(tenant_id=_T, campaign_id="c1"), pool=pool
    )
    assert out.campaign is not None
    assert out.campaign.transacting_count == 0
    assert out.complete is False
