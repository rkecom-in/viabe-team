#!/usr/bin/env python3
"""VT-65 PR-1 KG population canary (Rule #15, DR-15).

Backfills a seeded synthetic tenant → asserts the L1 KG fills correctly.

- A1: tenant/customer/transaction/campaign entities present after backfill
- A2: OWNS (tenant→customer) + MADE (customer→txn) + SENT (tenant→campaign) edges
- A3: customer phone is HASHED with the CANONICAL hash_phone (KG phone_hash ==
      hash_phone(phone)), and NO raw phone/name in the node (CL-390)
- A4: idempotent — a 2nd backfill adds no duplicate entities
- A5: tenant-scoped — a second tenant's KG is empty

Subshell-source supabase-dev.env (see vt196). CL-422: synthetic only.
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
INSERTED: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")


def _seed(pool: Any) -> tuple[str, str, str]:
    """Returns (tenant_id, customer_id, phone)."""
    tid, cid, rid = str(uuid4()), str(uuid4()), str(uuid4())
    phone = f"+9199{uuid4().int % 10**8:08d}"
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"vt65-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO customers (id, tenant_id, display_name, phone_e164) "
            "VALUES (%s, %s, %s, %s)",
            (cid, tid, "Ravi Kumar", phone),
        )
        conn.execute(
            "INSERT INTO imported_transactions "
            "(tenant_id, customer_id, source, provider_ref, amount_paise, direction, txn_date) "
            "VALUES (%s, %s, 'google_sheet', %s, 50000, 'credit', now()::date)",
            (tid, cid, f"ref-{uuid4().hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (rid, tid),
        )
        conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json) "
            "VALUES (%s, %s, 'proposed', now(), '{}'::jsonb)",
            (tid, rid),
        )
    INSERTED.append(tid)
    return tid, cid, phone


def _entities(pool: Any, tid: str) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT entity_type, external_key, attributes FROM l1_entities "
            "WHERE tenant_id = %s AND external_key IS NOT NULL",
            (tid,),
        ).fetchall()
    return [dict(r) for r in rows]


def _edge_types(pool: Any, tid: str) -> set[str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT relationship_type FROM l1_relationships WHERE tenant_id = %s",
            (tid,),
        ).fetchall()
    return {(r["relationship_type"] if isinstance(r, dict) else r[0]) for r in rows}


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

    from orchestrator.knowledge.kg_backfill import backfill_tenant
    from orchestrator.utils.phone_token import hash_phone

    tid, cid, phone = _seed(pool)
    tid_b, _, _ = _seed(pool)

    backfill_tenant(tid)
    ents = _entities(pool, tid)
    types = {e["entity_type"] for e in ents}
    a1 = {"tenant", "customer", "transaction", "campaign"} <= types
    assertion(1, "entities present (tenant/customer/transaction/campaign)", a1, observed=sorted(types))

    edges = _edge_types(pool, tid)
    a2 = {"owns", "made", "sent"} <= edges
    assertion(2, "edges present (owns/made/sent)", a2, observed=sorted(edges))

    cust = next((e for e in ents if e["entity_type"] == "customer"), None)
    cust_attrs = (cust or {}).get("attributes") or {}
    a3 = (
        cust is not None
        and cust_attrs.get("phone_hash") == hash_phone(phone)
        and phone not in str(cust_attrs)
        and "Ravi" not in str(cust_attrs)
    )
    assertion(3, "customer phone canonical-hashed; no raw phone/name (CL-390)", a3,
              observed={"phone_hash_matches": cust_attrs.get("phone_hash") == hash_phone(phone)})

    before = len(ents)
    backfill_tenant(tid)  # re-run
    after = len(_entities(pool, tid))
    assertion(4, "idempotent — 2nd backfill adds no dup entities", before == after,
              observed={"before": before, "after": after})

    a5 = len(_entities(pool, tid_b)) == 0
    assertion(5, "tenant-scoped — tenant B's KG empty (not backfilled)", a5,
              observed={"tenant_b_entities": len(_entities(pool, tid_b))})

    # cleanup
    with pool.connection() as conn:
        for t in INSERTED:
            conn.execute("DELETE FROM l1_relationships WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM l1_entities WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM kg_events_processed WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM imported_transactions WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM campaigns WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM pipeline_runs WHERE tenant_id = %s", (t,))
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
