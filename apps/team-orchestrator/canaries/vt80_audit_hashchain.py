#!/usr/bin/env python3
"""VT-80 privacy_audit_log hash-chain canary (Rule #15, DR-15).

Proves the tamper-evident chain + hard append-only against a REAL Postgres.

- A1: two log_privacy_event appends → chain links (row2.prev_hash==row1.this_hash)
- A2: verify_chain → ok over the appended rows
- A3: tamper (trigger disabled to simulate a DB-level attack) → verify_chain FAILS
- A4: append-only — UPDATE blocked by the immutability trigger
- A5: append-only — DELETE blocked by the immutability trigger
- A6: cross-tenant RLS — a tenant_connection sees only its own tenant's rows

Subshell-source supabase-dev.env (see vt196 for the pattern):

    cd apps/team-orchestrator
    ( set -a; source ../../.viabe/secrets/supabase-dev.env; set +a;
      ./.venv/bin/python canaries/vt80_audit_hashchain.py )
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


def assertion(num: int, name: str, passed: bool, *, observed: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK")


def _seed_tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt80-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod

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
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", os.environ["DATABASE_URL"])
    pool = graph_mod.get_pool()

    from orchestrator.observability.audit_log import list_events, log_privacy_event
    from orchestrator.observability.audit_verify import verify_chain

    tid = _seed_tenant(pool)

    # A1 — two appends chain.
    with pool.connection() as conn:
        h1 = log_privacy_event(
            conn, tenant_id=tid, event_type="phone_token_resolved",
            payload={"phone_token": "tok-1", "resolved": True}, actor="canary",
        )
        h2 = log_privacy_event(
            conn, tenant_id=tid, event_type="phone_token_resolved",
            payload={"phone_token": "tok-2", "resolved": False}, actor="canary",
        )
        events = list_events(conn, limit=2)
    by_hash = {e["this_hash"]: e for e in events}
    row1 = by_hash.get(h1)
    row2 = by_hash.get(h2)
    a1 = row2 is not None and row2["prev_hash"] == h1 and h1 != h2
    assertion(1, "two appends chain (row2.prev_hash == row1.this_hash)", a1,
              observed={"h1": h1[:12], "h2": h2[:12], "row2_prev": (row2 or {}).get("prev_hash", "")[:12]})

    # A2 — verify ok over THIS run's suffix (since_seq isolates from any leftover
    # rows so the canary is re-runnable against a non-pristine dev DB).
    seq1 = row1["seq"] if row1 else 1
    with pool.connection() as conn:
        v_ok = verify_chain(conn, since_seq=seq1)
    assertion(2, "verify_chain ok over this run's chain suffix", v_ok.ok, observed=v_ok)

    # A3 — tamper (disable trigger to simulate a DB-level attack) → verify fails.
    with pool.connection() as conn:
        conn.execute("ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_no_row_mutate")
        try:
            conn.execute(
                "UPDATE privacy_audit_log SET payload = %s::jsonb WHERE this_hash = %s",
                ('{"phone_token": "TAMPERED", "resolved": true}', h2),
            )
            v_bad = verify_chain(conn, since_seq=seq1)
        finally:
            conn.execute("ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_no_row_mutate")
    assertion(3, "tampered payload → verify_chain FAILS", not v_bad.ok, observed=v_bad)

    # A4 — UPDATE blocked by the immutability trigger.
    a4 = False
    with pool.connection() as conn:
        try:
            conn.execute("UPDATE privacy_audit_log SET actor = 'x' WHERE this_hash = %s", (h1,))
        except Exception as exc:  # noqa: BLE001
            a4 = "append-only" in str(exc)
    assertion(4, "UPDATE blocked (append-only trigger)", a4, observed="raised" if a4 else "NOT blocked")

    # A5 — DELETE blocked.
    a5 = False
    with pool.connection() as conn:
        try:
            conn.execute("DELETE FROM privacy_audit_log WHERE this_hash = %s", (h1,))
        except Exception as exc:  # noqa: BLE001
            a5 = "append-only" in str(exc)
    assertion(5, "DELETE blocked (append-only trigger)", a5, observed="raised" if a5 else "NOT blocked")

    # A6 — isolation by GRANT-EXCLUSION: app_role (tenant_connection) has NO
    # grant on privacy_audit_log (mig 007/008 predate the default-grant), so a
    # tenant context cannot read the audit log at all — the audit log is
    # service-role-only. That denial IS the cross-tenant isolation.
    from orchestrator.db import tenant_connection

    other_tid = _seed_tenant(pool)
    with pool.connection() as conn:
        log_privacy_event(conn, tenant_id=other_tid, event_type="phone_token_resolved",
                          payload={"phone_token": "tok-other"}, actor="canary")
    denied = False
    try:
        with tenant_connection(tid) as tconn:
            tconn.execute("SELECT 1 FROM privacy_audit_log LIMIT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        denied = "permission denied" in str(exc).lower()
    assertion(6, "tenant_connection denied reading privacy_audit_log (grant-exclusion isolation)",
              denied, observed="permission denied" if denied else "NOT denied (leak!)")

    # cleanup (service role; append-only blocks audit-row delete by design — we
    # disable the trigger to clean the synthetic rows, then drop the tenants).
    with pool.connection() as conn:
        conn.execute("ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_no_row_mutate")
        try:
            for t in INSERTED_TENANTS:
                conn.execute("DELETE FROM privacy_audit_log WHERE tenant_id = %s", (t,))
        finally:
            conn.execute("ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_no_row_mutate")
        for t in INSERTED_TENANTS:
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
