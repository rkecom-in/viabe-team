#!/usr/bin/env python3
"""VT-226 — webhook_metrics async DBOS workflow canary.

3 assertions:
- A1: invoking write_webhook_metric_workflow inserts a row within 1s
- A2: workflow returns shape {'status': 'written'} on success
- A3: source enum accepts all 4 expected values (twilio/razorpay/shopify/google_drive)

Real DB. Wall-clock ≤ 10s.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_SOURCE_IPS: list[str] = []


def assertion(num: int, name: str, passed: bool, *,
               observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK")


def _cleanup(pool: Any) -> None:
    if not INSERTED_SOURCE_IPS:
        return
    with pool.connection() as conn:
        for ip in INSERTED_SOURCE_IPS:
            conn.execute(
                "DELETE FROM webhook_metrics WHERE source_ip = %s",
                (ip,),
            )


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    from orchestrator.observability.webhook_metrics_writer import (
        write_webhook_metric_workflow,
    )

    # --- A1: invoke + verify row landed ---
    ip_a1 = f"127.0.{uuid4().int % 256}.1"
    INSERTED_SOURCE_IPS.append(ip_a1)

    t0 = time.perf_counter()
    r1 = write_webhook_metric_workflow(
        source="twilio", event="sig_pass",
        message_sid=f"SM_vt226_{uuid4().hex[:12]}",
        source_ip=ip_a1, response_status=200,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM webhook_metrics WHERE source_ip = %s",
            (ip_a1,),
        )
        row = cur.fetchone()
    count = int(row["n"] if isinstance(row, dict) else row[0])
    pass_1 = count == 1 and elapsed_ms < 1000.0
    assertion(
        1,
        "Invoke workflow → row written within 1s",
        pass_1,
        observed={"rows": count, "elapsed_ms": round(elapsed_ms, 1)},
    )

    # --- A2: shape ---
    pass_2 = r1.get("status") == "written" and r1.get("source") == "twilio"
    assertion(
        2,
        "Workflow returns shape {'status': 'written', ...}",
        pass_2,
        observed=r1,
    )

    # --- A3: all 4 source enum values accepted ---
    sources = ["twilio", "razorpay", "shopify", "google_drive"]
    enum_results = []
    for src in sources:
        ip = f"127.0.{uuid4().int % 256}.2"
        INSERTED_SOURCE_IPS.append(ip)
        r = write_webhook_metric_workflow(
            source=src, event="sig_pass",
            message_sid=None, source_ip=ip, response_status=200,
        )
        enum_results.append(r.get("status"))
    pass_3 = all(s == "written" for s in enum_results)
    assertion(
        3,
        "All 4 source enums accepted (twilio/razorpay/shopify/google_drive)",
        pass_3,
        observed={"results": enum_results},
    )

    _cleanup(pool)
    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
