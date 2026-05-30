#!/usr/bin/env python3
"""VT-43 — get_attribution_data canary.

Mock-mode CI default. Real dev-DB mode opt-in via VT43_REAL_DB=1 seeds
SYNTHETIC data only (CL-422: no real customer phones / payment IDs /
ledger rows / message bodies) — fabricated tenant + campaign + 3
attributions, runs the real SQL aggregation, verifies the snapshot
shape + reproducibility, then deletes the synthetic rows.

4 assertions:
- A1: XOR input validation (campaign_id vs window; both/neither reject)
- A2: campaign-mode aggregate (status + transacting_count + arrr_paise)
- A3: reproducibility — two identical calls byte-identical model_dump_json
- A4: real dev-DB seed → aggregate matches seeded ARRR (VT43_REAL_DB=1)
      OR mock-mode equivalent

Wall-clock ≤ 10s.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
T0 = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
SEEDED_TENANTS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _mock_pool_campaign(*, campaign_row: Any, agg_row: Any) -> Any:
    cur = MagicMock()
    fetchone_q = [campaign_row, agg_row]
    cur.execute.side_effect = lambda sql, params=None: None
    cur.fetchone.side_effect = lambda: fetchone_q.pop(0) if fetchone_q else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def _real_pool() -> Any:
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    return graph_mod.get_pool()


def _seed_synthetic(pool: Any) -> tuple[str, str]:
    """Seed a synthetic tenant + closed campaign + 3 attributions
    (fabricated payment IDs — NO real customer data, CL-422)."""
    tenant_id = str(uuid4())
    campaign_id = str(uuid4())
    SEEDED_TENANTS.append(tenant_id)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL app.current_tenant = %s", (tenant_id,))
        cur.execute(
            """
            INSERT INTO tenants (id, business_name, plan_tier, phase)
            VALUES (%s, %s, 'founding', 'paid_active')
            """,
            (tenant_id, f"vt43-synthetic-{tenant_id[:8]}"),
        )
        cur.execute(
            """
            INSERT INTO campaigns
                (id, tenant_id, run_id, subscriber_id, template_id,
                 body_params, status, proposed_at, proposed_by,
                 attribution_close_at, attribution_closed_at)
            VALUES (%s, %s, gen_random_uuid(), gen_random_uuid(),
                    'vt43_synthetic_template', '{}'::jsonb, 'sent',
                    now(), 'canary', %s, %s)
            """,
            (campaign_id, tenant_id, T1, T1),
        )
        for paise in (1000, 2000, 3000):
            cur.execute(
                """
                INSERT INTO attributions
                    (tenant_id, campaign_id, razorpay_payment_id,
                     attributed_paise)
                VALUES (%s, %s, %s, %s)
                """,
                (tenant_id, campaign_id, f"pay_synthetic_{uuid4().hex[:12]}",
                 paise),
            )
    return tenant_id, campaign_id


def _cleanup(pool: Any) -> None:
    if not SEEDED_TENANTS:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for tid in SEEDED_TENANTS:
            cur.execute("SET LOCAL app.current_tenant = %s", (tid,))
            cur.execute("DELETE FROM attributions WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM campaigns WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    real = os.environ.get("VT43_REAL_DB") == "1"
    if real and not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — VT43_REAL_DB=1 needs DATABASE_URL",
              file=sys.stderr)
        return 2
    print(f"PREFLIGHT OK (mode={'real-db' if real else 'mock'})")

    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )

    # --- A1: XOR validation ---
    rejects = 0
    for kwargs in (
        {"tenant_id": "t", "campaign_id": "c", "window_start": T0},
        {"tenant_id": "t"},
        {"tenant_id": "t", "window_start": T0},
    ):
        try:
            GetAttributionDataInput(**kwargs)  # type: ignore[arg-type]
        except Exception:
            rejects += 1
    ok_campaign = ok_window = False
    try:
        GetAttributionDataInput(tenant_id="t", campaign_id="c")
        GetAttributionDataInput(tenant_id="t", window_start=T0, window_end=T1)
        ok_campaign = ok_window = True
    except Exception:
        pass
    assertion(
        1, "XOR validation (3 bad reject, 2 good accept)",
        rejects == 3 and ok_campaign and ok_window,
        observed={"rejects": rejects, "good_accepted": ok_campaign and ok_window},
    )

    # --- A2 + A4: aggregate ---
    if real:
        pool = _real_pool()
        try:
            tenant_id, campaign_id = _seed_synthetic(pool)
            out = get_attribution_data(
                GetAttributionDataInput(tenant_id=tenant_id, campaign_id=campaign_id),
                pool=pool,
            )
            pass_2 = (
                out.campaign is not None
                and out.campaign.attribution_status == "closed"
                and out.campaign.transacting_count == 3
                and out.campaign.arrr_paise == 6000
            )
            assertion(
                2, "Real dev-DB: seeded 3 attributions → ARRR=6000, status=closed",
                pass_2,
                observed={
                    "arrr": out.campaign.arrr_paise if out.campaign else None,
                    "transacting": out.campaign.transacting_count if out.campaign else None,
                    "status": out.campaign.attribution_status if out.campaign else None,
                },
            )
            # A3 reproducibility on real data
            r1 = get_attribution_data(
                GetAttributionDataInput(tenant_id=tenant_id, campaign_id=campaign_id),
                pool=pool,
            ).model_dump_json()
            r2 = get_attribution_data(
                GetAttributionDataInput(tenant_id=tenant_id, campaign_id=campaign_id),
                pool=pool,
            ).model_dump_json()
            assertion(3, "Reproducibility — two real queries byte-identical",
                      r1 == r2, observed={"identical": r1 == r2})
            assertion(4, "Real seed→aggregate path exercised", pass_2,
                      observed={"real_db": True})
        finally:
            _cleanup(pool)
    else:
        pool = _mock_pool_campaign(
            campaign_row={"attribution_close_at": T1,
                          "attribution_closed_at": T1, "total_arrr_paise": 6000},
            agg_row={"transacting_count": 3, "arrr_paise": 6000},
        )
        out = get_attribution_data(
            GetAttributionDataInput(tenant_id="t", campaign_id="c1"), pool=pool
        )
        pass_2 = (
            out.campaign is not None
            and out.campaign.attribution_status == "closed"
            and out.campaign.transacting_count == 3
            and out.campaign.arrr_paise == 6000
        )
        assertion(
            2, "Mock: campaign aggregate (status=closed, count=3, ARRR=6000)",
            pass_2,
            observed={"arrr": out.campaign.arrr_paise if out.campaign else None},
        )

        def _run() -> str:
            p = _mock_pool_campaign(
                campaign_row={"attribution_close_at": T1,
                              "attribution_closed_at": T1, "total_arrr_paise": 6000},
                agg_row={"transacting_count": 3, "arrr_paise": 6000},
            )
            return get_attribution_data(
                GetAttributionDataInput(tenant_id="t", campaign_id="c1"), pool=p
            ).model_dump_json()

        assertion(3, "Reproducibility — two calls byte-identical",
                  _run() == _run(), observed={"identical": _run() == _run()})
        assertion(4, "Degraded fields None (Pillar 7 honest, not 0)",
                  out.campaign is not None
                  and out.campaign.cohort_size is None
                  and out.campaign.attribution_rate is None,
                  observed={"cohort": out.campaign.cohort_size if out.campaign else "?"})

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)",
              file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
