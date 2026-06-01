#!/usr/bin/env python3
"""VT-273 ledger write-path canary (Rule #15 / DR-15).

PURE (default): entry_key determinism + enum gate.
REAL DB (VT273_REAL_DB=1): synthetic — write N entries → read back → RE-ingest
(idempotent, 0 new) → low-confidence deferred → cross-tenant FK block →
resolve_customer_by_phone_token returns the right customer (N2). FAIL-NOT-SKIP if
VT273_REAL_DB=1 but DATABASE_URL absent. CL-422 synthetic only.

    cd apps/team-orchestrator
    uv run --no-project --with pydantic python canaries/vt273_ledger.py        # pure
    DATABASE_URL=postgres://... VT273_REAL_DB=1 uv run python canaries/vt273_ledger.py  # real DB
"""

from __future__ import annotations

import os
import sys
from datetime import date
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
    from orchestrator.integrations.dedup_merge import AcquiredViaError
    from orchestrator.integrations.ledger import (
        LedgerEntryIn,
        _entry_key,
        record_ledger_entries,
    )

    def entry(conf=0.9, amount=150000, etype="sale"):
        return LedgerEntryIn(amount_paise=amount, entry_type=etype,
                             entry_date=date(2026, 6, 1), confidence=conf)

    t, c = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    key_ok = (_entry_key(t, c, entry()) == _entry_key(t, c, entry())
              and _entry_key(t, c, entry()) != _entry_key(t, c, entry(amount=1)))
    assertion("A1", "entry_key deterministic + distinct on amount", key_ok)

    rejected = False
    try:
        record_ledger_entries(t, c, [entry()], acquired_via="nope")
    except AcquiredViaError:
        rejected = True
    assertion("A2", "invalid acquired_via REJECTED", rejected)

    if os.environ.get("VT273_REAL_DB", "0") != "1":
        return _finalise(False)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("PREFLIGHT FAIL (real DB) — DATABASE_URL absent. Fail-not-skip.", file=sys.stderr)
        return 2

    import apply_migrations
    import psycopg

    if apply_migrations.apply(dsn=dsn)["failed"]:
        print("PREFLIGHT FAIL — migrations", file=sys.stderr)
        return 2
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.db import tenant_connection
        from orchestrator.integrations.dedup_merge import dedup_and_merge
        from orchestrator.integrations.ledger import resolve_customer_by_phone_token
        from orchestrator.observability.phone_tokens import _hash_phone

        def tenant():
            with psycopg.connect(dsn, autocommit=True) as cn:
                return str(cn.execute(
                    "INSERT INTO tenants (business_name, plan_tier, phase) VALUES "
                    "('VT-273 canary','founding','onboarding') RETURNING id").fetchone()[0])

        ta, tb = tenant(), tenant()
        phone = "+9190" + uuid4().int.__str__()[:8]
        ra = dedup_and_merge(ta, acquired_via="paper_book", phone_e164=phone)
        cust = str(ra.customer_id)
        es = [entry(0.9, 150000, "sale"), entry(0.88, 5000, "payment")]

        r1 = record_ledger_entries(ta, cust, es, acquired_via="paper_book")
        r2 = record_ledger_entries(ta, cust, es, acquired_via="paper_book")  # re-ingest
        with tenant_connection(ta) as conn:
            n = conn.execute("SELECT count(*) AS n FROM customer_ledger_entries WHERE customer_id=%s",
                             (cust,)).fetchone()["n"]
        assertion("A3", "write 2 → re-ingest idempotent (0 new) → exactly 2 rows",
                  r1.written == 2 and r2.written == 0 and r2.skipped_duplicate == 2 and n == 2,
                  observed={"r1": r1.written, "r2_dup": r2.skipped_duplicate, "rows": n})

        rlow = record_ledger_entries(ta, cust, [entry(conf=0.5)], acquired_via="paper_book")
        assertion("A4", "low-confidence entry deferred, not written (P4)",
                  rlow.written == 0 and rlow.deferred_low_confidence == 1, observed=vars(rlow))

        fk_blocked = False
        try:
            record_ledger_entries(tb, cust, [entry()], acquired_via="paper_book")
        except psycopg.Error:
            fk_blocked = True
        assertion("A5", "cross-tenant: B cannot write to A's customer (composite FK)", fk_blocked)

        tok = _hash_phone(phone)
        assertion("A6", "resolve_customer_by_phone_token returns A's customer; B→None (N2)",
                  resolve_customer_by_phone_token(ta, tok) == ra.customer_id
                  and resolve_customer_by_phone_token(tb, tok) is None,
                  observed={"a": str(resolve_customer_by_phone_token(ta, tok)),
                            "b": resolve_customer_by_phone_token(tb, tok)})
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
