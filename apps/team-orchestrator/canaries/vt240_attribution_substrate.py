#!/usr/bin/env python3
"""VT-240 — attribution method/confidence substrate canary.

Verifies the provenance substrate end-to-end: the deterministic mapper
(match_basis → attribution_method) is reproducible, match_transactions emits
the method on real matches, and migration 047's columns + CHECKs behave on the
real dev DB (populated insert round-trips; out-of-range confidence + bad method
rejected; pre-047-shape insert survives with NULLs).

Mock-mode CI default (A1 + A2 — pure mapper + in-memory match, no DB). Real
dev-DB mode opt-in via VT240_REAL_DB=1 (A3 + A4) seeds SYNTHETIC data ONLY
(CL-422: fabricated tenant 'vt240-syn-*'; attribution amounts are synthetic
paise, no real ledger), then cleans up.

4 assertions:
- A1: mapper is exhaustive + DETERMINISTIC (Fazal day-39 reproducibility gate)
  — every match_basis → its method, identical across repeated calls, never
  emits manual_owner.
- A2: match_transactions stamps attribution_method on declared matches
  (vpa → exact_match; amount[/time] → window_match).
- A3: real 047 columns — a populated attribution round-trips (method +
  confidence) and a pre-047-shape insert (both omitted) persists as NULL.
- A4: real CHECKs — manual_owner accepted; bad method + out-of-range
  confidence rejected.

Wall-clock <= 10s.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
SEEDED_TENANTS: list[str] = []
T0 = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
              expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


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


def _scalar(row: Any) -> Any:
    if row is None:
        return None
    return row["id"] if isinstance(row, dict) and "id" in row else (
        list(row.values())[0] if isinstance(row, dict) else row[0]
    )


def _cleanup(pool: Any) -> None:
    if not SEEDED_TENANTS:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for tid in SEEDED_TENANTS:
            cur.execute("DELETE FROM attributions WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM campaigns WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM pipeline_runs WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    real = os.environ.get("VT240_REAL_DB") == "1"
    if real and not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — VT240_REAL_DB=1 needs DATABASE_URL", file=sys.stderr)
        return 2
    print(f"PREFLIGHT OK (mode={'real-db' if real else 'mock'})")

    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        attribution_method_from_match_basis,
        match_transactions,
    )

    # --- A1: deterministic, exhaustive mapper ---
    expected = {
        "amount": "window_match",
        "amount+time": "window_match",
        "amount+vpa": "exact_match",
        "amount+time+vpa": "exact_match",
    }
    map_ok = all(
        attribution_method_from_match_basis(b) == m for b, m in expected.items()
    )
    # determinism: same input → same output across repeats.
    det_ok = all(
        len({attribution_method_from_match_basis(b) for _ in range(8)}) == 1
        for b in expected
    )
    no_manual = all(
        attribution_method_from_match_basis(b) != "manual_owner"
        for b in (*expected, "none")
    )
    pass_1 = map_ok and det_ok and no_manual
    assertion(1, "Mapper exhaustive + deterministic + never manual_owner", pass_1,
              observed={"map_ok": map_ok, "deterministic": det_ok,
                        "no_manual_owner": no_manual})

    # --- A2: match_transactions stamps the method on declared matches ---
    payload = MatchTransactionsInput(
        tenant_id="t1",
        transactions=[
            TransactionInput(txn_id="VPA1", amount_paise=15000, timestamp=T0,
                             vpa="payer@upi"),
            TransactionInput(txn_id="AMT1", amount_paise=22000, timestamp=T0),
        ],
    )
    ledger = [
        {"id": "L1", "amount_paise": 15000, "entry_ts": T0, "ref_vpa": "payer@upi"},
        {"id": "L2", "amount_paise": 22000, "entry_ts": T0, "ref_vpa": None},
    ]
    out = match_transactions(payload, candidate_ledger=ledger)
    by_txn = {m.txn_id: m for m in out.matches}
    pass_2 = (
        by_txn["VPA1"].attribution_method == "exact_match"
        and by_txn["AMT1"].attribution_method == "window_match"
    )
    assertion(2, "match_transactions stamps method (vpa→exact, amount→window)",
              pass_2, observed={"VPA1": by_txn.get("VPA1") and by_txn["VPA1"].attribution_method,
                                "AMT1": by_txn.get("AMT1") and by_txn["AMT1"].attribution_method})

    if real:
        import psycopg

        pool = _real_pool()
        try:
            tid = str(uuid4())
            SEEDED_TENANTS.append(tid)
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (id, business_name, plan_tier, phase) "
                    "VALUES (%s, %s, 'founding', 'paid_active')",
                    (tid, f"vt240-syn-{tid[:8]}"),
                )
                cur.execute(
                    "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                    "VALUES (%s, 'campaign', 'running') RETURNING id", (tid,))
                run = _scalar(cur.fetchone())
                cur.execute(
                    "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, "
                    "generated_at) VALUES (%s, %s, '{}'::jsonb, 'proposed', now()) "
                    "RETURNING id", (tid, run))
                camp = _scalar(cur.fetchone())

                # A3: populated round-trip + pre-047-shape NULL survival.
                cur.execute(
                    "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                    "attribution_method, attribution_confidence) "
                    "VALUES (%s, %s, 50000, 'exact_match', 0.87) RETURNING id",
                    (tid, camp))
                attr = _scalar(cur.fetchone())
                cur.execute(
                    "SELECT attribution_method, attribution_confidence "
                    "FROM attributions WHERE id = %s", (attr,))
                row = cur.fetchone()
                method = row["attribution_method"] if isinstance(row, dict) else row[0]
                conf = row["attribution_confidence"] if isinstance(row, dict) else row[1]

                cur.execute(
                    "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
                    "VALUES (%s, %s, 12000) RETURNING id", (tid, camp))
                legacy = _scalar(cur.fetchone())
                cur.execute(
                    "SELECT attribution_method, attribution_confidence "
                    "FROM attributions WHERE id = %s", (legacy,))
                lrow = cur.fetchone()
                lmethod = lrow["attribution_method"] if isinstance(lrow, dict) else lrow[0]
                lconf = lrow["attribution_confidence"] if isinstance(lrow, dict) else lrow[1]

            pass_3 = (
                method == "exact_match" and abs(float(conf) - 0.87) < 1e-4
                and lmethod is None and lconf is None
            )
            assertion(3, "047 columns: populated round-trips; pre-047 insert NULL-survives",
                      pass_3, observed={"method": method, "confidence": conf,
                                        "legacy_method": lmethod, "legacy_conf": lconf})

            # A4: CHECK enforcement — manual_owner OK; bad method + bad conf rejected.
            manual_ok = False
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                    "attribution_method) VALUES (%s, %s, 1, 'manual_owner')", (tid, camp))
                manual_ok = True

            def _rejected(sql: str, params: tuple) -> bool:
                try:
                    with pool.connection() as conn, conn.cursor() as cur:
                        cur.execute(sql, params)
                    return False
                except psycopg.Error:
                    return True

            bad_method_rej = _rejected(
                "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                "attribution_method) VALUES (%s, %s, 1, 'bogus')", (tid, camp))
            hi_conf_rej = _rejected(
                "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                "attribution_confidence) VALUES (%s, %s, 1, 1.5)", (tid, camp))
            lo_conf_rej = _rejected(
                "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise, "
                "attribution_confidence) VALUES (%s, %s, 1, -0.1)", (tid, camp))
            pass_4 = manual_ok and bad_method_rej and hi_conf_rej and lo_conf_rej
            assertion(4, "047 CHECKs: manual_owner OK; bad method + out-of-range conf rejected",
                      pass_4, observed={"manual_owner_ok": manual_ok,
                                        "bad_method_rejected": bad_method_rej,
                                        "conf>1_rejected": hi_conf_rej,
                                        "conf<0_rejected": lo_conf_rej})
        finally:
            _cleanup(pool)
            from orchestrator import graph as graph_mod
            if graph_mod._pool is not None:
                graph_mod._pool.close()
                graph_mod._pool = None
    else:
        assertion(3, "047 column round-trip (real-mode only) — skipped in mock",
                  True, observed={"mode": "mock"})
        assertion(4, "047 CHECK enforcement (real-mode only) — skipped in mock",
                  True, observed={"mode": "mock"})

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
