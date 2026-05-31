#!/usr/bin/env python3
"""VT-54 dedup + merge canary (Rule #15 / DR-15).

PURE (default): the enum gate (invalid acquired_via REJECTED).
REAL DB (VT54_REAL_DB=1): synthetic 2-method merge on live pg — insert via
paper_book, merge same phone via contacts -> ONE row, acquired_via both tags,
non-overwrite; cross-tenant denial with a REAL count backstop. FAIL-NOT-SKIP if
VT54_REAL_DB=1 but DATABASE_URL absent. CL-422: synthetic phones only.

    cd apps/team-orchestrator
    uv run --no-project --with pydantic python canaries/vt54_dedup_merge.py        # pure
    DATABASE_URL=postgres://... VT54_REAL_DB=1 uv run python canaries/vt54_dedup_merge.py  # real DB
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[str, dict[str, Any]] = {}


def assertion(key: str, name: str, passed: bool, *, observed=None) -> None:
    RESULTS[key] = {"name": name, "status": "PASS" if passed else "FAIL"}
    print(f"[{key}] {'PASS' if passed else 'FAIL'} — {name}")
    print(f"    observed: {observed}")


def run() -> int:
    from orchestrator.integrations.dedup_merge import (
        AcquiredViaError,
        dedup_and_merge,
    )

    # A1 — enum gate (deterministic, both modes).
    rejected = False
    try:
        dedup_and_merge("11111111-1111-4111-8111-111111111111",
                        acquired_via="not_a_method", phone_e164="+919000000001")
    except AcquiredViaError:
        rejected = True
    assertion("A1", "invalid acquired_via REJECTED (single-source enum gate)",
              rejected, observed={"rejected": rejected})

    if os.environ.get("VT54_REAL_DB", "0") != "1":
        return _finalise(False)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("PREFLIGHT FAIL (real DB) — DATABASE_URL absent. Fail-not-skip.",
              file=sys.stderr)
        return 2

    import apply_migrations
    import psycopg

    r = apply_migrations.apply(dsn=dsn)
    if r["failed"]:
        print(f"PREFLIGHT FAIL — migrations: {r['failed']}", file=sys.stderr)
        return 2
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.db import tenant_connection

        def _tenant() -> str:
            with psycopg.connect(dsn, autocommit=True) as c:
                return str(c.execute(
                    "INSERT INTO tenants (business_name, plan_tier, phase) VALUES "
                    "('VT-54 canary', 'founding', 'onboarding') RETURNING id"
                ).fetchone()[0])

        ta, tb = _tenant(), _tenant()
        phone = "+9190" + uuid4().int.__str__()[:8]

        r1 = dedup_and_merge(ta, acquired_via="paper_book", phone_e164=phone, display_name="Asha")
        r2 = dedup_and_merge(ta, acquired_via="contacts", phone_e164=phone, display_name="Other")
        with tenant_connection(ta) as conn:
            row = conn.execute(
                "SELECT count(*) AS n, max(display_name) AS nm FROM customers "
                "WHERE phone_e164 = %s", (phone,)).fetchone()
        assertion("A2", "2-method merge -> ONE row, both tags, non-overwrite name",
                  r2.kind == "merged" and r2.customer_id == r1.customer_id
                  and r2.acquired_via == ("contacts", "paper_book")
                  and row["n"] == 1 and row["nm"] == "Asha",
                  observed={"kind": r2.kind, "tags": r2.acquired_via, "rows": row["n"], "name": row["nm"]})

        rb = dedup_and_merge(tb, acquired_via="contacts", phone_e164=phone)
        with tenant_connection(tb) as conn:
            leak = conn.execute(
                "SELECT count(*) AS n FROM customers WHERE id = %s",
                (str(r1.customer_id),)).fetchone()["n"]
        assertion("A3", "cross-tenant: B fresh-inserts, sees 0 of A's row (RLS)",
                  rb.kind == "inserted" and rb.customer_id != r1.customer_id and leak == 0,
                  observed={"b_kind": rb.kind, "b_sees_a": leak})
    finally:
        shutdown_dbos()
    return _finalise(True)


def _finalise(real_db: bool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for k, r in RESULTS.items():
        print(f"  [{k}] {r['status']} — {r['name']}")
    print(f"\n=== mode: {'REAL DB' if real_db else 'PURE (no DB)'} ===")
    failed = [k for k, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
