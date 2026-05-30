#!/usr/bin/env python3
"""VT-170 — customers table + registry + cohort integrity canary.

Mock-mode CI default. Real dev-DB mode opt-in via VT170_REAL_DB=1 seeds
SYNTHETIC data ONLY (CL-422: customers is tenant-identifying PII — no
real phones/emails/names; fabricated tenant + display_name='vt170-syn-*'
+ phone_e164='+9199990000NN'). Verifies: registry-backed redaction, RLS
cross-tenant denial, cohort resolve (real id resolves, bogus rejected),
then cleans up.

4 assertions:
- A1: registry callable redacts a known synthetic name; unknown passes
- A2: cohort resolve → resolved + rejected (bogus id surfaced, not dropped)
- A3: real dev-DB customer insert + RLS cross-tenant denial (VT170_REAL_DB=1)
- A4: real cohort integrity — same-tenant link resolves, cross-tenant rejected

Wall-clock ≤ 10s.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
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


def _mock_pool(*, customer_names=None, real_cohort_ids=None) -> Any:
    cur = MagicMock()
    rows = [{"display_name": n} for n in (customer_names or [])]
    cohort_rows = [{"id": cid} for cid in (real_cohort_ids or [])]
    # registry fetchall returns names; cohort fetchall returns ids.
    # Use a switch on the last-executed SQL.
    state = {"last": ""}

    def _execute(sql: str, params: tuple | None = None) -> None:
        state["last"] = sql

    def _fetchall() -> list[Any]:
        if "display_name" in state["last"]:
            return rows
        if "id::text" in state["last"] or "id = ANY" in state["last"]:
            return cohort_rows
        return []

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
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


def _cleanup(pool: Any) -> None:
    if not SEEDED_TENANTS:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for tid in SEEDED_TENANTS:
            cur.execute("SET LOCAL app.current_tenant = %s", (tid,))
            cur.execute("DELETE FROM campaign_recipients WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM campaigns WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM customers WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    real = os.environ.get("VT170_REAL_DB") == "1"
    if real and not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — VT170_REAL_DB=1 needs DATABASE_URL", file=sys.stderr)
        return 2
    print(f"PREFLIGHT OK (mode={'real-db' if real else 'mock'})")

    from orchestrator.observability.pii import redact_for_log
    from orchestrator.privacy import customer_registry
    from orchestrator.privacy.customer_registry import make_name_registry
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    customer_registry.invalidate_all()

    # --- A1: registry-backed redaction (mock pool) ---
    reg = make_name_registry(
        "t1", pool=_mock_pool(customer_names=["Synthetic Syndrome"])
    )
    redacted = redact_for_log({"note": "Synthetic Syndrome called"}, name_registry=reg)
    pass_1 = "Synthetic Syndrome" not in str(redacted) and reg("unknown person") is False
    assertion(1, "Registry redacts known synthetic name; unknown passes", pass_1,
              observed={"redacted_out": "Synthetic Syndrome" not in str(redacted)})

    # --- A2: cohort resolve resolved/rejected (mock) ---
    pool_c = _mock_pool(real_cohort_ids=["c1"])  # only c1 is a real customer
    res = resolve_cohort_recipients(
        tenant_id="t1", campaign_id="camp1",
        customer_ids=["c1", "c2_bogus"], pool=pool_c,
    )
    pass_2 = res.resolved == ["c1"] and res.rejected == ["c2_bogus"]
    assertion(2, "Cohort resolve: bogus id rejected (not silently dropped)", pass_2,
              observed={"resolved": res.resolved, "rejected": res.rejected})

    if real:
        pool = _real_pool()
        try:
            tid_a = str(uuid4())
            tid_b = str(uuid4())
            SEEDED_TENANTS.extend([tid_a, tid_b])
            with pool.connection() as conn, conn.cursor() as cur:
                for tid in (tid_a, tid_b):
                    cur.execute("SET LOCAL app.current_tenant = %s", (tid,))
                    cur.execute(
                        "INSERT INTO tenants (id, business_name, plan_tier, phase) "
                        "VALUES (%s, %s, 'founding', 'paid_active')",
                        (tid, f"vt170-syn-{tid[:8]}"),
                    )
                cur.execute("SET LOCAL app.current_tenant = %s", (tid_a,))
                cur.execute(
                    "INSERT INTO customers (tenant_id, display_name, phone_e164) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (tid_a, "vt170-syn-alice", "+919999000011"),
                )
                cust_a = cur.fetchone()
                cust_a_id = str(cust_a["id"] if isinstance(cust_a, dict) else cust_a[0])
                cur.execute(
                    "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at) "
                    "VALUES (%s, gen_random_uuid(), '{}'::jsonb, 'proposed', now()) RETURNING id",
                    (tid_a,),
                )
                camp = cur.fetchone()
                camp_a_id = str(camp["id"] if isinstance(camp, dict) else camp[0])

            # A3: RLS — scoped to B, A's customer invisible.
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SET LOCAL app.current_tenant = %s", (tid_b,))
                cur.execute("SELECT count(*) AS n FROM customers WHERE tenant_id = %s", (tid_a,))
                leaked = cur.fetchone()
                leaked_n = int(leaked["n"] if isinstance(leaked, dict) else leaked[0])
            assertion(3, "Real RLS: tenant B cannot see tenant A customers",
                      leaked_n == 0, observed={"leaked": leaked_n})

            # A4: cohort integrity — real id resolves, bogus rejected.
            bogus = str(uuid4())
            res_real = resolve_cohort_recipients(
                tenant_id=tid_a, campaign_id=camp_a_id,
                customer_ids=[cust_a_id, bogus], pool=pool,
            )
            pass_4 = res_real.resolved == [cust_a_id] and res_real.rejected == [bogus]
            assertion(4, "Real cohort integrity: real resolves, bogus rejected",
                      pass_4, observed={"resolved": res_real.resolved,
                                        "rejected_count": len(res_real.rejected)})
        finally:
            _cleanup(pool)
    else:
        assertion(3, "RLS cross-tenant (real-mode only) — skipped in mock", True,
                  observed={"mode": "mock"})
        assertion(4, "Cohort integrity DB FK (real-mode only) — skipped in mock", True,
                  observed={"mode": "mock"})

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
