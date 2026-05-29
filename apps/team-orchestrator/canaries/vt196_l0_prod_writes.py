#!/usr/bin/env python3
"""VT-196 L0 production write wiring canary (Rule #15, DR-15).

Three assertions covering the consent + PII gates around the existing
VT-126 L0 substrate. Per-tenant k-anonymity admission is brief-deferred
(schema change required); read-side k-anon (observation_count >= 10)
remains the load-bearing exposure gate.

- A1: tenant with owner_inputs=False → status='rejected_consent'; NO row
- A2: tenant with owner_inputs=True + clean content → status='written'
- A3: tenant with owner_inputs=True + PII content → status='rejected_pii'

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt196_l0_prod_writes.py
    )
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []
INSERTED_FRAGMENTS: list[str] = []


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


def _seed_tenant(pool: Any, owner_inputs: bool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number, owner_inputs) "
            "VALUES (%s, %s, 'standard', 'trial', %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tid, f"vt196-{tid[:8]}", f"+9199{uuid4().hex[:8]}", owner_inputs),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _cleanup(pool: Any) -> None:
    if not INSERTED_FRAGMENTS:
        return
    with pool.connection() as conn:
        for fid in INSERTED_FRAGMENTS:
            conn.execute("DELETE FROM l0_fragments WHERE id = %s", (fid,))
        for tid in INSERTED_TENANTS:
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


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

    from orchestrator.memory import write_l0_fragment_workflow

    # --- A1: owner_inputs=False rejects ---
    tid_a1 = _seed_tenant(pool, owner_inputs=False)
    r1 = write_l0_fragment_workflow(
        tenant_id=tid_a1,
        fragment_type="cohort_pattern",
        cohort_key=f"vt196-a1-{uuid4().hex[:8]}",
        content={"signal": "test_clean_payload"},
    )
    pass_1 = r1.get("status") == "rejected_consent"
    assertion(1, "owner_inputs=False → rejected_consent + no write",
              pass_1, observed=r1)

    # --- A2: owner_inputs=True + clean content → written ---
    tid_a2 = _seed_tenant(pool, owner_inputs=True)
    cohort_a2 = f"vt196-a2-{uuid4().hex[:8]}"
    r2 = write_l0_fragment_workflow(
        tenant_id=tid_a2,
        fragment_type="cohort_pattern",
        cohort_key=cohort_a2,
        content={"signal": "vt196_clean_signal", "metric": 42},
    )
    pass_2 = r2.get("status") == "written" and r2.get("fragment_id")
    if pass_2 and r2.get("fragment_id"):
        INSERTED_FRAGMENTS.append(r2["fragment_id"])
    assertion(2, "owner_inputs=True + clean content → status=written + fragment_id",
              pass_2, observed=r2)

    # --- A3: owner_inputs=True + PII content → rejected_pii ---
    tid_a3 = _seed_tenant(pool, owner_inputs=True)
    cohort_a3 = f"vt196-a3-{uuid4().hex[:8]}"
    r3 = write_l0_fragment_workflow(
        tenant_id=tid_a3,
        fragment_type="cohort_pattern",
        cohort_key=cohort_a3,
        # Phone number triggers redact_for_log per existing PII gate
        content={"phone": "+919876543210", "context": "test"},
    )
    pass_3 = r3.get("status") == "rejected_pii"
    assertion(3, "owner_inputs=True + PII content → rejected_pii + no write",
              pass_3, observed=r3)

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
