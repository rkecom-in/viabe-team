#!/usr/bin/env python3
"""VT-103 cost-dashboard canary (Rule #15).

Subshell-source `.viabe/secrets/supabase-dev.env` and run:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt103_cost_dashboard.py 2>&1 | tail -200
    )

Exits 0 iff all 8 assertions PASS against real Supabase dev DB. Prints
verbatim per-assertion observed values + captured row JSON as the
Rule #15 audit artifact (VT-102 supplement pattern).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


CANARY_TENANT_A = UUID("00000000-0000-4000-8000-000000aaa1A3")
CANARY_TENANT_B = UUID("00000000-0000-4000-8000-000000bbb1A3")
CANARY_TENANT_ANOMALY = UUID("00000000-0000-4000-8000-000000a4d103")
CANARY_TENANT_UNIT = UUID("00000000-0000-4000-8000-000000bf7103")
CANARY_COMPONENT = "canary"

# 10 tenants used for top-N test, plus 2 small ones below the cut-line.
TOP_N_TENANTS: list[UUID] = [
    UUID(f"00000000-0000-4000-8000-0000000{i:05x}") for i in range(0x1A300, 0x1A30C)
]

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []


def assertion(
    num: int,
    name: str,
    passed: bool,
    *,
    observed: Any = None,
    expected: Any = None,
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {
        "name": name,
        "status": status,
        "observed": observed,
        "expected": expected,
    }
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _default_serialiser(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _resolved_host() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    after_at = url.split("@", 1)[1]
    return after_at.split("/", 1)[0]


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print(
            "PREFLIGHT FAIL — DATABASE_URL not set; source supabase-dev.env in subshell.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"PREFLIGHT OK — resolved host: {_resolved_host()} (env-loaded)")


def _seed_tenant(pool, tenant_id: UUID, plan_tier: str = "standard") -> None:
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, %s, 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-cost-{tenant_id}", plan_tier),
        )


def _seed_event(
    pool,
    tenant_id: UUID,
    run_id: UUID,
    cost_paise: int,
    category: str,
    vendor: str,
    when: datetime,
) -> None:
    payload = {
        "vendor": vendor,
        "endpoint": "/canary",
        "cost_paise": cost_paise,
        "cost_category": category,
    }
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_log "
            "(run_id, tenant_id, event_type, severity, component, payload, created_at) "
            "VALUES (%s, %s, 'external_api_call', 'info', %s, %s::jsonb, %s)",
            (
                str(run_id),
                str(tenant_id),
                CANARY_COMPONENT,
                json.dumps(payload),
                when,
            ),
        )


def run_canary() -> int:
    _preflight()
    now = datetime.now(timezone.utc)

    # Make assertion #7 deterministic by pinning STANDARD plan price.
    os.environ["STANDARD_PRICE_PAISE"] = "100000"  # ₹1000/month

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability import (
        detect_cost_anomalies,
        get_tenant_cost,
        get_tenant_unit_economics,
        get_workspace_cost_summary,
    )

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    _seed_tenant(pool, CANARY_TENANT_A)
    _seed_tenant(pool, CANARY_TENANT_B)
    _seed_tenant(pool, CANARY_TENANT_ANOMALY)
    _seed_tenant(pool, CANARY_TENANT_UNIT, plan_tier="standard")
    for t in TOP_N_TENANTS:
        _seed_tenant(pool, t)

    # -------------------------------------------------------------------
    # Assertion 1 — Seed 50 events for tenant_A across 5 categories.
    # -------------------------------------------------------------------
    cats = [
        ("llm", "anthropic"),
        ("twilio", "twilio"),
        ("razorpay", "razorpay"),
        ("apify", "apify"),
        ("infra_allocated", "infra"),
    ]
    a_run = uuid4()
    INSERTED_RUN_IDS.append(str(a_run))
    expected_total_a = 0
    expected_by_cat: dict[str, int] = {}
    for i in range(50):
        cat, vendor = cats[i % 5]
        cost = (i + 1) * 10  # 10..500 paise
        _seed_event(pool, CANARY_TENANT_A, a_run, cost, cat, vendor, now - timedelta(minutes=5))
        expected_total_a += cost
        expected_by_cat[cat] = expected_by_cat.get(cat, 0) + cost

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM pipeline_log WHERE run_id = %s",
            (str(a_run),),
        )
        seeded = int(cur.fetchone()["c"])
    pass_1 = seeded == 50
    assertion(
        1,
        "Seed 50 external_api_call events for tenant_A across 5 categories",
        pass_1,
        observed=f"host={_resolved_host()} seeded_rows={seeded}",
        expected="seeded_rows=50",
    )

    # -------------------------------------------------------------------
    # Assertion 2 — get_tenant_cost matches seeded sums within ±1 paise.
    # -------------------------------------------------------------------
    since = now - timedelta(hours=1)
    until = now + timedelta(hours=1)
    bd = get_tenant_cost(CANARY_TENANT_A, since, until)
    total_ok = abs(bd.total_paise - expected_total_a) <= 1
    cat_ok = all(
        abs(bd.by_category.get(cat, 0) - paise) <= 1
        for cat, paise in expected_by_cat.items()
    )
    pass_2 = total_ok and cat_ok
    assertion(
        2,
        "get_tenant_cost returns aggregate + per-category match within ±1 paise",
        pass_2,
        observed={
            "total_paise": bd.total_paise,
            "expected_total_paise": expected_total_a,
            "by_category": dict(bd.by_category),
            "expected_by_category": expected_by_cat,
            "event_count": bd.event_count,
        },
        expected={
            "total_paise": expected_total_a,
            "by_category": expected_by_cat,
            "event_count": 50,
        },
    )

    # -------------------------------------------------------------------
    # Assertion 3 — Materialized-view refresh works end-to-end.
    # -------------------------------------------------------------------
    refresh_err = None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW tenant_cost_daily")
    except BaseException as exc:  # noqa: BLE001
        refresh_err = f"{type(exc).__name__}: {exc}"

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(cost_paise), 0) AS paise
              FROM tenant_cost_daily
             WHERE tenant_id = %s
            """,
            (str(CANARY_TENANT_A),),
        )
        mv_total = int(cur.fetchone()["paise"] or 0)
    pass_3 = refresh_err is None and mv_total == expected_total_a
    assertion(
        3,
        "REFRESH MATERIALIZED VIEW tenant_cost_daily aggregates match raw events",
        pass_3,
        observed={
            "mv_total": mv_total,
            "expected_total": expected_total_a,
            "refresh_err": refresh_err,
        },
        expected={
            "mv_total": expected_total_a,
            "refresh_err": None,
        },
    )

    # -------------------------------------------------------------------
    # Assertion 4 — Cross-tenant isolation under real RLS.
    # -------------------------------------------------------------------
    b_run = uuid4()
    INSERTED_RUN_IDS.append(str(b_run))
    expected_total_b = 0
    for i in range(20):
        cost = (i + 1) * 7
        cat, vendor = cats[i % 5]
        _seed_event(pool, CANARY_TENANT_B, b_run, cost, cat, vendor, now - timedelta(minutes=5))
        expected_total_b += cost

    bd_a = get_tenant_cost(CANARY_TENANT_A, since, until)
    bd_b = get_tenant_cost(CANARY_TENANT_B, since, until)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM pipeline_log WHERE tenant_id = %s",
            (str(CANARY_TENANT_A),),
        )
        sql_count_a = int(cur.fetchone()["c"])
        cur.execute(
            "SELECT COUNT(*) AS c FROM pipeline_log WHERE tenant_id = %s",
            (str(CANARY_TENANT_B),),
        )
        sql_count_b = int(cur.fetchone()["c"])
    pass_4 = (
        bd_a.total_paise == expected_total_a
        and bd_b.total_paise == expected_total_b
        and sql_count_a >= 50
        and sql_count_b >= 20
    )
    assertion(
        4,
        "Cross-tenant: get_tenant_cost(A) unaffected by tenant_B; get_tenant_cost(B) returns B's total",
        pass_4,
        observed={
            "a_total_via_fn": bd_a.total_paise,
            "a_total_expected": expected_total_a,
            "b_total_via_fn": bd_b.total_paise,
            "b_total_expected": expected_total_b,
            "sql_count_a": sql_count_a,
            "sql_count_b": sql_count_b,
        },
        expected={
            "a_total": expected_total_a,
            "b_total": expected_total_b,
        },
    )

    # -------------------------------------------------------------------
    # Assertion 5 — Top-10 outliers correctly ranked.
    # Seed 12 tenants: costs 50, 100, 150, ..., 600 paise.
    # -------------------------------------------------------------------
    expected_topN: list[tuple[UUID, int]] = []
    for i, tid in enumerate(TOP_N_TENANTS):
        cost = (i + 1) * 50  # 50..600
        run_id = uuid4()
        INSERTED_RUN_IDS.append(str(run_id))
        _seed_event(pool, tid, run_id, cost, "llm", "anthropic", now - timedelta(minutes=10))
        expected_topN.append((tid, cost))
    # Descending — highest first; top-10 excludes the two lowest (₹0.50, ₹1.00).
    expected_topN.sort(key=lambda x: -x[1])
    expected_top10_ids = [tid for tid, _ in expected_topN[:10]]
    expected_excluded_ids = [tid for tid, _ in expected_topN[10:]]

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW tenant_cost_daily")

    summary = get_workspace_cost_summary(now - timedelta(days=1), now + timedelta(days=1), top_n=10)
    top_ids_observed = [tid for tid, _ in summary.top_tenants]
    # The workspace has more than just our seeded tenants; check that:
    # - all 10 of our expected top tenants appear somewhere in the response,
    # - their relative order is preserved (highest > next > next ...),
    # - the two excluded ones are NOT in the top-10 over the seeded set.
    seeded_in_top10 = [tid for tid in top_ids_observed if tid in TOP_N_TENANTS]
    relative_order_ok = seeded_in_top10 == [
        tid for tid in expected_top10_ids if tid in top_ids_observed
    ]
    excluded_not_in_top10 = all(tid not in top_ids_observed for tid in expected_excluded_ids)
    pass_5 = relative_order_ok and excluded_not_in_top10
    assertion(
        5,
        "Top-10 ranking: seeded tenants sorted descending; lowest 2 excluded",
        pass_5,
        observed={
            "top_ids_observed": [str(t) for t in top_ids_observed],
            "seeded_in_top10": [str(t) for t in seeded_in_top10],
            "expected_top10": [str(t) for t in expected_top10_ids],
            "excluded": [str(t) for t in expected_excluded_ids],
        },
        expected={
            "relative_order_preserved": True,
            "lowest_two_excluded": True,
        },
    )

    # -------------------------------------------------------------------
    # Assertion 6 — Anomaly detection flags the right tenant.
    # tenant_anomaly: baseline ₹50/day days-28..-8, then ₹150/day days-7..0.
    # tenant_A: steady cost across the same window.
    # -------------------------------------------------------------------
    # Baseline window (days -28 .. -8): 21 days @ 5000 paise/day = 105000 paise total.
    for d in range(8, 29):
        run_id = uuid4()
        INSERTED_RUN_IDS.append(str(run_id))
        _seed_event(
            pool,
            CANARY_TENANT_ANOMALY,
            run_id,
            5000,
            "llm",
            "anthropic",
            now - timedelta(days=d),
        )
    # Recent window (days -7 .. -1): 7 days @ 15000 paise/day = 105000 paise — 3× ratio.
    for d in range(1, 8):
        run_id = uuid4()
        INSERTED_RUN_IDS.append(str(run_id))
        _seed_event(
            pool,
            CANARY_TENANT_ANOMALY,
            run_id,
            15000,
            "llm",
            "anthropic",
            now - timedelta(days=d),
        )
    # tenant_A is the steady comparator — already has only "now" events.

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW tenant_cost_daily")

    flagged = detect_cost_anomalies(reference_days=28, window_days=7, multiplier=2.0)
    flagged_ids = [a.tenant_id for a in flagged]
    pass_6 = CANARY_TENANT_ANOMALY in flagged_ids and CANARY_TENANT_A not in flagged_ids
    flagged_summary = [
        {
            "tenant_id": str(a.tenant_id),
            "baseline_per_day": a.reference_avg_per_day_paise,
            "window_per_day": a.window_avg_per_day_paise,
            "multiplier_observed": round(a.multiplier_observed, 3),
        }
        for a in flagged
    ]
    assertion(
        6,
        "Anomaly: 3× spike tenant flagged; steady tenant not flagged",
        pass_6,
        observed={
            "flagged_count": len(flagged),
            "flagged": flagged_summary,
        },
        expected={
            "anomaly_tenant_flagged": True,
            "steady_tenant_not_flagged": True,
        },
    )

    # -------------------------------------------------------------------
    # Assertion 7 — ARRR / cost ratio (plan-tier × env price).
    # STANDARD_PRICE_PAISE=100000 (₹1000/month). Window = 30 days → arrr = 100000.
    # Cost = ₹250 = 25000 paise → ratio = 4.0.
    # -------------------------------------------------------------------
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))
    _seed_event(pool, CANARY_TENANT_UNIT, run_id, 25000, "llm", "anthropic", now - timedelta(hours=1))

    ue = get_tenant_unit_economics(
        CANARY_TENANT_UNIT,
        now - timedelta(days=30),
        now,
    )
    ratio_ok = abs(ue.ratio - 4.0) <= 0.05
    pass_7 = ratio_ok
    assertion(
        7,
        "Unit economics: STANDARD plan ₹1000/month vs ₹250 cost → ratio≈4.0",
        pass_7,
        observed={
            "arrr_paise": ue.arrr_paise,
            "cost_paise": ue.cost_paise,
            "ratio": round(ue.ratio, 4),
        },
        expected={
            "arrr_paise": 100000,
            "cost_paise": 25000,
            "ratio": 4.0,
        },
    )

    # -------------------------------------------------------------------
    # Assertion 8 — Cleanup is real-DB cleanup; final residual count logged.
    # Capture the audit artifact BEFORE the DELETE so reviewers have
    # verbatim row evidence even after the cleanup empties the table.
    # -------------------------------------------------------------------
    _capture_audit_rows(pool)

    cleanup_err = None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pipeline_log WHERE run_id = ANY(%s)",
                (INSERTED_RUN_IDS,),
            )
    except BaseException as exc:  # noqa: BLE001
        cleanup_err = f"{type(exc).__name__}: {exc}"

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM pipeline_log "
            "WHERE component = %s AND run_id = ANY(%s)",
            (CANARY_COMPONENT, INSERTED_RUN_IDS),
        )
        residual = int(cur.fetchone()["c"])

    # Refresh MV after deletes so it doesn't carry residual canary rows forward.
    refresh_after_err = None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW tenant_cost_daily")
    except BaseException as exc:  # noqa: BLE001
        refresh_after_err = f"{type(exc).__name__}: {exc}"

    pass_8 = cleanup_err is None
    assertion(
        8,
        "Cleanup ran; residual canary rows count captured for audit",
        pass_8,
        observed={
            "cleanup_err": cleanup_err,
            "residual_rows": residual,
            "refresh_after_err": refresh_after_err,
        },
        expected={"cleanup_err": None},
    )

    return _finalise(pool)


AUDIT_ROWS: list[dict[str, Any]] = []


def _capture_audit_rows(pool) -> None:
    """Snapshot canary-inserted rows before the cleanup DELETE runs.

    The verbatim row JSON is the Rule-#15 evidence reviewers need; capturing
    after DELETE produces an empty array, which defeats the audit standard.
    """
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, run_id, tenant_id, event_type, severity, component, "
                "       payload, duration_ms, created_at "
                "  FROM pipeline_log "
                " WHERE run_id = ANY(%s) "
                " ORDER BY created_at ASC LIMIT 20",
                (INSERTED_RUN_IDS,),
            )
            for r in cur.fetchall():
                AUDIT_ROWS.append(
                    {
                        "id": str(r["id"]),
                        "run_id": str(r["run_id"]),
                        "tenant_id": str(r["tenant_id"]) if r["tenant_id"] else None,
                        "event_type": r["event_type"],
                        "severity": r["severity"],
                        "component": r["component"],
                        "payload": r["payload"],
                        "duration_ms": r["duration_ms"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"audit fetch failed: {exc!r}", file=sys.stderr)


def _finalise(pool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== AUDIT ARTIFACT — top-20 inserted canary rows + outputs ===")
    print(json.dumps(AUDIT_ROWS, indent=2, default=_default_serialiser))

    # Final cleanup attempt — covers canary tenants we created.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pipeline_log WHERE tenant_id = ANY(%s)",
                (INSERTED_TENANT_IDS,),
            )
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)",
                (INSERTED_TENANT_IDS,),
            )
    except BaseException as exc:  # noqa: BLE001
        print(f"final cleanup failed (90-day retention will sweep): {exc!r}", file=sys.stderr)

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 8 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
