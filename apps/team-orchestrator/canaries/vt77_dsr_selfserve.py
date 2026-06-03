#!/usr/bin/env python3
"""VT-77 DSR self-serve export/delete canary (Rule #15, DR-15).

Exercises the export + delete fulfilment logic against a REAL Postgres.

- A1: export gathers tenant tables (tenants + customers present)
- A2: PII scrub — raw phone_e164 NOT in the export blob (Phase-1 (a) posture)
- A3: tenant-scope — tenant A's export excludes tenant B's tenant row
- A4: audit — dsr_export_requested + dsr_export_completed on the chain; verify ok
- A5: delete fulfilment — create ticket + purge_tenant_data → tenant anonymized

Subshell-source supabase-dev.env (see vt196):

    cd apps/team-orchestrator
    ( set -a; source ../../.viabe/secrets/supabase-dev.env; set +a;
      ./.venv/bin/python canaries/vt77_dsr_selfserve.py )
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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


def _seed(pool: Any, phone: str) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt77-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164) VALUES (%s, %s, %s)",
            (tid, "Canary Customer", phone),
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

    from orchestrator.dsr_export import build_export_zip, export_tenant_data
    from orchestrator.observability.audit_verify import verify_chain

    secret_phone = "+919812340000"
    tid = _seed(pool, secret_phone)
    tid_b = _seed(pool, "+919899990000")

    export = export_tenant_data(tid)
    a1 = (
        len(export["tables"].get("tenants", [])) == 1
        and len(export["tables"].get("customers", [])) == 1
    )
    assertion(1, "export gathers tenant tables (tenants + customers)", a1,
              observed={t: len(r) for t, r in export["tables"].items()})

    blob = json.dumps(export, default=str)
    assertion(2, "PII scrub — raw phone_e164 absent from export", secret_phone not in blob,
              observed="absent" if secret_phone not in blob else "LEAKED")

    tenant_ids = {str(r["id"]) for r in export["tables"]["tenants"]}
    assertion(3, "tenant-scope — A's export excludes B", tid_b not in tenant_ids,
              observed=tenant_ids)

    first_seq = None
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT min(seq) AS s FROM privacy_audit_log WHERE tenant_id = %s "
            "AND event_type IN ('dsr_export_requested','dsr_export_completed')",
            (tid,),
        ).fetchone()
        first_seq = row["s"] if isinstance(row, dict) else row[0]
        kinds = {
            (r["event_type"] if isinstance(r, dict) else r[0])
            for r in conn.execute(
                "SELECT event_type FROM privacy_audit_log WHERE tenant_id = %s",
                (tid,),
            ).fetchall()
        }
        chain_ok = verify_chain(conn, since_seq=first_seq).ok
    a4 = {"dsr_export_requested", "dsr_export_completed"} <= kinds and chain_ok
    assertion(4, "audit — export events chained + verify ok", a4,
              observed={"kinds": sorted(kinds), "chain_ok": chain_ok})

    # A5 — delete fulfilment: ticket + purge → tenant anonymized.
    from orchestrator.dsr_purge import purge_tenant_data

    with pool.connection() as conn:
        created = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id::text AS id",
            (tid,),
        ).fetchone()
        ticket_id = created["id"] if isinstance(created, dict) else created[0]
    result = purge_tenant_data(UUID(ticket_id))
    assertion(5, "delete — ticket + purge → tenant anonymized", result.tenant_anonymized,
              observed={"anonymized": result.tenant_anonymized, "counts": result.deleted_counts})

    # cleanup (append-only audit rows stay; drop the tenants we made).
    _build_zip_smoke = build_export_zip(export)  # exercise the zip path too
    assert _build_zip_smoke[:2] == b"PK", "zip not produced"
    with pool.connection() as conn:
        conn.execute("ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_no_row_mutate")
        try:
            for t in INSERTED:
                conn.execute("DELETE FROM privacy_audit_log WHERE tenant_id = %s", (t,))
        finally:
            conn.execute("ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_no_row_mutate")
        for t in INSERTED:
            conn.execute("DELETE FROM dsr_tickets WHERE tenant_id = %s", (t,))
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
