#!/usr/bin/env python3
"""VT-72 typed tenant-scoped wrapper canary (Rule #15, DR-15).

- A1: CustomersWrapper.insert forces tenant_id + roundtrips (find_by_id)
- A2: cross-tenant isolation — tenant B cannot see tenant A's row
- A3: validation primitive raises TenantIsolationError on a mismatched row
- A4: delete is tenant-scoped (B can't delete A's row; A can)
- A5: no-direct-tenant-db-access lint passes on the current tree

Subshell-source supabase-dev.env (see vt196).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")


def _tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt72-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    INSERTED.append(tid)
    return tid


def run_canary() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK")

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", os.environ["DATABASE_URL"])
    pool = graph_mod.get_pool()

    from orchestrator._tenant_guard import TenantIsolationError
    from orchestrator.db.wrappers import CustomersWrapper

    w = CustomersWrapper()
    tid_a = _tenant(pool)
    tid_b = _tenant(pool)

    row = w.insert(tid_a, {"display_name": "Canary A", "tenant_id": str(uuid4())})
    a1 = str(row.get("tenant_id")) == tid_a and w.find_by_id(tid_a, row["id"]) is not None
    assertion(1, "insert forces tenant_id + roundtrips", a1, observed={"tenant_id": str(row.get("tenant_id"))})

    a2 = w.find_by_id(tid_b, row["id"]) is None and all(
        str(r["id"]) != str(row["id"]) for r in w.list_for_tenant(tid_b)
    )
    assertion(2, "cross-tenant isolation (B can't see A)", a2, observed="isolated" if a2 else "LEAK")

    raised = False
    try:
        w._validate([{"tenant_id": uuid4(), "id": uuid4()}], uuid4())
    except TenantIsolationError:
        raised = True
    assertion(3, "validation primitive raises on mismatch", raised, observed="raised" if raised else "NO raise")

    a4 = w.delete(tid_b, row["id"]) == 0 and w.delete(tid_a, row["id"]) == 1
    assertion(4, "delete is tenant-scoped", a4, observed=a4)

    repo = Path(__file__).resolve().parents[3]
    lint = subprocess.run(
        [sys.executable, "scripts/check_no_direct_tenant_db_access.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assertion(5, "no-direct-tenant-db-access lint passes", lint.returncode == 0,
              observed=lint.stdout.strip() or lint.stderr.strip())

    with pool.connection() as conn:
        for t in INSERTED:
            conn.execute("DELETE FROM customers WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM tenants WHERE id = %s", (t,))
    graph_mod._pool.close()
    graph_mod._pool = None

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
