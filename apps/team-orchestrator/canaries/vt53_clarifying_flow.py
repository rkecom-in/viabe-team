#!/usr/bin/env python3
"""VT-53 clarifying-question flow canary (Rule #15 / DR-15).

PURE (default): deterministic reply-parsing assertions (no DB).
REAL DB (VT53_REAL_DB=1): real Postgres round-trip on a SYNTHETIC tenant
(CL-422) — open -> record_reply -> verify answered; open overdue -> sweep ->
verify expired; cross-tenant resolve denied (RLS). FAIL-NOT-SKIP: if
VT53_REAL_DB=1 but DATABASE_URL is absent, exits non-zero.

    cd apps/team-orchestrator
    uv run --no-project --with pydantic python canaries/vt53_clarifying_flow.py          # pure
    DATABASE_URL=postgres://... VT53_REAL_DB=1 uv run python canaries/vt53_clarifying_flow.py   # real DB
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[str, dict[str, Any]] = {}


def assertion(key: str, name: str, passed: bool, *, observed=None) -> None:
    RESULTS[key] = {"name": name, "status": "PASS" if passed else "FAIL"}
    print(f"[{key}] {'PASS' if passed else 'FAIL'} — {name}")
    print(f"    observed: {observed}")


def run() -> int:
    from orchestrator.integrations.clarifying_flow import (
        parse_amount_to_paise,
        parse_numeric,
    )

    # A1 — deterministic parsing (Devanagari + English words + currency).
    cases = {"1500": 1500, "१५००": 1500, "fifteen hundred": 1500,
             "₹1,500": 1500, "two thousand five hundred": 2500, "no idea": None}
    p_ok = all(parse_numeric(k) == v for k, v in cases.items())
    assertion("A1", "deterministic numeric parsing (ascii/devanagari/words/₹)", p_ok,
              observed={k: parse_numeric(k) for k in cases})
    assertion("A2", "amount->paise (₹1500 -> 150000)",
              parse_amount_to_paise("₹1500") == 150000 and parse_amount_to_paise("x") is None,
              observed={"₹1500": parse_amount_to_paise("₹1500")})

    real_db = os.environ.get("VT53_REAL_DB", "0") == "1"
    if not real_db:
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
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.db import tenant_connection
        from orchestrator.integrations.clarifying_flow import (
            ClarificationQuestion,
            open_clarification,
            record_reply,
            sweep_expired,
        )

        def _tenant() -> str:
            with psycopg.connect(dsn, autocommit=True) as c:
                return str(c.execute(
                    "INSERT INTO tenants (business_name, plan_tier, phase) VALUES "
                    "('VT-53 canary', 'founding', 'onboarding') RETURNING id"
                ).fetchone()[0])

        ta, tb = _tenant(), _tenant()
        cid = open_clarification(ta, "upload", [ClarificationQuestion(field="bal", prompt="?")])
        recorded = record_reply(ta, cid, {"bal": 150000})
        assertion("A3", "open -> record_reply resolves (status answered)",
                  recorded is True, observed={"recorded": recorded})

        open_clarification(
            ta, "old", [ClarificationQuestion(field="x", prompt="?")],
            now=datetime.now(UTC) - timedelta(days=8))
        n = sweep_expired(ta, now=datetime.now(UTC))
        assertion("A4", "sweep_expired marks overdue 'expired' (P4 drop, not commit)",
                  n >= 1, observed={"expired": n})

        denied = record_reply(tb, cid, {"bal": 1})
        with tenant_connection(tb) as conn:
            leak = conn.execute(
                "SELECT count(*) AS n FROM pending_clarifications WHERE id=%s",
                (str(cid),)).fetchone()["n"]
        assertion("A5", "cross-tenant: B cannot resolve/see A's clarification (RLS)",
                  denied is False and leak == 0, observed={"denied": not denied, "b_sees": leak})
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
