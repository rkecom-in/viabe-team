"""VT-42 — get_recent_campaigns tests.

CI stdlib-only smoke skips via importorskip("langchain").
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("langchain")


def _undefined_table_exc() -> Exception:
    return type("UndefinedTable", (Exception,), {})("relation does not exist")


@contextmanager
def _patch_campaigns(campaign_rows: list[Any] | None = None, *, missing: bool = False):
    """VT-306: get_recent_campaigns now reads via CampaignsWrapper. Patch the
    wrapper method to return the staged rows (or raise UndefinedTable) — replaces
    the old mock-pool fetchall seam (a non-UUID test tenant can't hit the real
    wrapper's _uuid/tenant_connection)."""
    def _fn(self: Any, tenant_id: Any, *, days_back: int, limit: int, conn: Any = None) -> list[Any]:
        if missing:
            raise _undefined_table_exc()
        return list(campaign_rows or [])

    with patch(
        "orchestrator.agent.tools.get_recent_campaigns.CampaignsWrapper.list_recent_with_responses",
        _fn,
    ):
        yield


def test_pydantic_io_shape() -> None:
    from orchestrator.agent.tools.get_recent_campaigns import (
        CampaignRollup,
        GetRecentCampaignsInput,
        GetRecentCampaignsOutput,
    )
    inp = GetRecentCampaignsInput(tenant_id="t1", days_back=14, limit=50)
    assert inp.days_back == 14

    out = GetRecentCampaignsOutput(
        campaigns=[
            CampaignRollup(
                campaign_id="c1",
                sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                template_id="welcome",
                recipients_count=1,
                response_count=0,
                status="sent",
            ),
        ],
    )
    assert len(out.campaigns) == 1


def test_returns_empty_when_table_missing() -> None:
    if os.environ.get("VT42_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )
    with _patch_campaigns(missing=True):
        result = get_recent_campaigns(GetRecentCampaignsInput(tenant_id="t1"))
    assert result.campaigns == []


def test_returns_rollups_with_response_counts() -> None:
    if os.environ.get("VT42_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )
    rows = [
        {
            "campaign_id": "c2",
            "sent_at": datetime(2026, 5, 28, tzinfo=timezone.utc),
            "template_id": "promo_v2",
            "status": "sent",
            "response_count": 3,
        },
        {
            "campaign_id": "c1",
            "sent_at": datetime(2026, 5, 24, tzinfo=timezone.utc),
            "template_id": "promo_v1",
            "status": "sent",
            "response_count": 0,
        },
    ]
    with _patch_campaigns(rows):
        result = get_recent_campaigns(GetRecentCampaignsInput(tenant_id="t1", days_back=7))
    assert len(result.campaigns) == 2
    assert result.campaigns[0].campaign_id == "c2"
    assert result.campaigns[0].response_count == 3
    assert result.campaigns[0].recipients_count == 1
    assert result.campaigns[1].response_count == 0


def test_input_rejects_bad_bounds() -> None:
    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
    )
    with pytest.raises(ValueError):
        GetRecentCampaignsInput(tenant_id="t1", days_back=0)
    with pytest.raises(ValueError):
        GetRecentCampaignsInput(tenant_id="t1", days_back=400)
    with pytest.raises(ValueError):
        GetRecentCampaignsInput(tenant_id="t1", limit=0)
    with pytest.raises(ValueError):
        GetRecentCampaignsInput(tenant_id="t1", limit=300)


def test_returns_empty_when_no_rows() -> None:
    if os.environ.get("VT42_REAL_DB") == "1":
        pytest.skip("real-DB mode active")

    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )
    with _patch_campaigns([]):
        result = get_recent_campaigns(GetRecentCampaignsInput(tenant_id="t1"))
    assert result.campaigns == []
