#!/usr/bin/env python3
"""VT-42 — get_recent_campaigns canary.

3 assertions (mock-mode CI; real-mode VT42_REAL_DB=1):
- A1: Pydantic IO validates + rejects bad bounds
- A2: Match path returns ordered rollups with response counts
- A3: Schema-absent path returns empty list gracefully

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
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


def _undefined_table_exc() -> Exception:
    return type("UndefinedTable", (Exception,), {})("relation does not exist")


def _fake_pool(*, campaign_rows=None, campaigns_table_missing=False):
    cur = MagicMock()

    def _execute(sql: str, _p: tuple | None = None) -> None:
        if campaigns_table_missing and "FROM campaigns" in sql:
            raise _undefined_table_exc()

    cur.execute.side_effect = _execute
    cur.fetchall.return_value = campaign_rows or []
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
    if os.environ.get("VT42_REAL_DB") == "1":
        if not os.environ.get("DATABASE_URL"):
            print("PREFLIGHT FAIL — VT42_REAL_DB=1 needs DATABASE_URL",
                  file=sys.stderr)
            return 2
    print("PREFLIGHT OK")

    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )

    # --- A1: IO ---
    ok_input = False
    rejects = 0
    try:
        GetRecentCampaignsInput(tenant_id="t1", days_back=7, limit=20)
        ok_input = True
    except Exception:
        pass
    for kwargs in (
        {"tenant_id": "t1", "days_back": 0},
        {"tenant_id": "t1", "days_back": 400},
        {"tenant_id": "t1", "limit": 0},
        {"tenant_id": "t1", "limit": 300},
    ):
        try:
            GetRecentCampaignsInput(**kwargs)  # type: ignore[arg-type]
        except Exception:
            rejects += 1
    assertion(
        1,
        "IO accepts valid + rejects 4 bad bound cases",
        ok_input and rejects == 4,
        observed={"valid_accepted": ok_input, "bad_rejected": rejects},
    )

    # --- A2: rollups with response counts ---
    pool = _fake_pool(
        campaign_rows=[
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
        ],
    )
    r = get_recent_campaigns(
        GetRecentCampaignsInput(tenant_id="t1"), pool=pool,
    )
    pass_2 = (
        len(r.campaigns) == 2
        and r.campaigns[0].campaign_id == "c2"
        and r.campaigns[0].response_count == 3
        and r.campaigns[0].recipients_count == 1
        and r.campaigns[1].response_count == 0
    )
    assertion(
        2,
        "Rollups returned with response counts + recipients_count=1",
        pass_2,
        observed={
            "count": len(r.campaigns),
            "first_id": r.campaigns[0].campaign_id if r.campaigns else None,
            "first_responses": (
                r.campaigns[0].response_count if r.campaigns else None
            ),
        },
    )

    # --- A3: schema absent → empty ---
    pool = _fake_pool(campaigns_table_missing=True)
    r = get_recent_campaigns(
        GetRecentCampaignsInput(tenant_id="t1"), pool=pool,
    )
    pass_3 = r.campaigns == []
    assertion(
        3,
        "Campaigns table missing → empty list gracefully",
        pass_3,
        observed={"count": len(r.campaigns)},
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
