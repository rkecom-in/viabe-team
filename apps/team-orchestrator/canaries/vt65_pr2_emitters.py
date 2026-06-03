#!/usr/bin/env python3
"""VT-65 PR-2 live-emitter canary (Rule #15, DR-15).

Proves the transactional outbox + live emitters on the LIVE call path.

- A1: commit → outbox row (same txn) → drain → KG entity present
- A2: ROLLBACK → NO outbox row + NO KG entity (atomicity — the headline)
- A3: idempotent — re-drain adds no duplicate KG entity
- A4: real multi-write site — dedup_and_merge (customer) → drain → KG customer
      node, phone canonical-hashed (no raw PII)
- A5: scheduled-context site — close_attribution → KG campaign arrr_paise aggregate

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


def _tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"vt65p2-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    INSERTED.append(tid)
    return tid


def _ent_count(pool: Any, tid: str, etype: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM l1_entities WHERE tenant_id = %s AND entity_type = %s",
            (tid, etype),
        ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


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

    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event

    tid = _tenant(pool)

    # A1 — commit → outbox → drain → KG entity.
    ck = str(uuid4())
    with tenant_connection(tid) as conn, conn.transaction():
        emit_kg_event(conn, "customer_created", tid, {"customer_id": ck})
    drain_kg_events(tid)
    a1 = _ent_count(pool, tid, "customer") == 1
    assertion(1, "commit → outbox → drain → KG entity", a1, observed=_ent_count(pool, tid, "customer"))

    # A2 — rollback → no outbox row + no KG entity (atomicity).
    rk = str(uuid4())
    try:
        with tenant_connection(tid) as conn, conn.transaction():
            emit_kg_event(conn, "customer_created", tid, {"customer_id": rk})
            raise RuntimeError("forced rollback")
    except RuntimeError:
        pass
    drain_kg_events(tid)
    with pool.connection() as conn:
        outbox_n = conn.execute(
            "SELECT count(*) AS n FROM kg_events WHERE tenant_id = %s AND payload->>'customer_id' = %s",
            (tid, rk),
        ).fetchone()
        kg_n = conn.execute(
            "SELECT count(*) AS n FROM l1_entities WHERE tenant_id = %s AND external_key = %s",
            (tid, rk),
        ).fetchone()
    a2 = (outbox_n["n"] if isinstance(outbox_n, dict) else outbox_n[0]) == 0 and \
         (kg_n["n"] if isinstance(kg_n, dict) else kg_n[0]) == 0
    assertion(2, "rollback → NO outbox row + NO KG entity (atomicity)", a2,
              observed={"outbox": outbox_n, "kg": kg_n})

    # A3 — idempotent re-drain.
    before = _ent_count(pool, tid, "customer")
    drain_kg_events(tid)
    assertion(3, "idempotent re-drain (no dup)", _ent_count(pool, tid, "customer") == before,
              observed={"before": before, "after": _ent_count(pool, tid, "customer")})

    # A4 — real multi-write site: dedup_and_merge → customer node, phone hashed.
    from orchestrator.integrations.dedup_merge import dedup_and_merge
    from orchestrator.utils.phone_token import hash_phone

    phone = f"+9198{uuid4().int % 10**8:08d}"
    res = dedup_and_merge(tid, acquired_via="owner_typed", phone_e164=phone, display_name="Asha")
    with pool.connection() as conn:
        cust = conn.execute(
            "SELECT attributes FROM l1_entities WHERE tenant_id = %s AND entity_type = 'customer' "
            "AND external_key = %s", (tid, str(res.customer_id)),
        ).fetchone()
    attrs = (cust["attributes"] if isinstance(cust, dict) else cust[0]) if cust else {}
    a4 = cust is not None and attrs.get("phone_hash") == hash_phone(phone) and phone not in str(attrs)
    assertion(4, "real site dedup_and_merge → KG customer, canonical-hash no-PII", a4,
              observed={"has_node": cust is not None})

    # A5 — scheduled-context site: close_attribution → KG campaign arrr_paise.
    rid, camp = str(uuid4()), str(uuid4())
    with pool.connection() as conn:
        conn.execute("INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
                     "VALUES (%s,%s,'twilio_inbound','completed')", (rid, tid))
        conn.execute("INSERT INTO campaigns (id, tenant_id, run_id, status, generated_at, plan_json) "
                     "VALUES (%s,%s,%s,'sent', now(), '{}'::jsonb)", (camp, tid, rid))
        conn.execute("INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
                     "VALUES (%s,%s,12345)", (tid, camp))
    from orchestrator.billing.attribution_close import close_attribution

    close_attribution(camp)
    with pool.connection() as conn:
        camp_node = conn.execute(
            "SELECT attributes FROM l1_entities WHERE tenant_id = %s AND entity_type='campaign' "
            "AND external_key = %s", (tid, camp),
        ).fetchone()
    cattrs = (camp_node["attributes"] if isinstance(camp_node, dict) else camp_node[0]) if camp_node else {}
    a5 = camp_node is not None and int(cattrs.get("arrr_paise") or 0) == 12345
    assertion(5, "scheduled-context close_attribution → KG campaign arrr_paise", a5,
              observed={"arrr_paise": cattrs.get("arrr_paise")})

    # cleanup
    with pool.connection() as conn:
        for t in INSERTED:
            for tbl in ("l1_relationships", "l1_entities", "kg_events", "kg_events_processed",
                        "attributions", "pipeline_log", "pipeline_steps", "campaigns",
                        "pipeline_runs", "customers", "phone_token_resolutions"):
                conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (t,))  # noqa: S608 — fixed list
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
