"""VT-556 teach→pickup PROOF on deployed dev (run via `railway run` so the dev DB URL + the
MANAGER_MEMORY_RETRIEVAL flag inject OS-env→process — never into CC's context).

Proves the human-as-teacher loop end-to-end against the LIVE dev Supabase, short of a live manager
dispatch (which needs an inbound trigger — flagged as the residual gap):
  1. ingest a VTR directive (upsert_directive — the same store path the API endpoint calls)
  2. it persisted with provenance (authority='vtr' + authored_by_operator_id) + retrieval_eligible
  3. get_active_memory(agent='manager') RETURNS it on live dev (retrieval works)
  4. dispatch._build_manager_directive_block renders the `## VTR directives` block (flag ON on dev)

Creates + DELETES a clearly-bogus test tenant (NOT real customer data — CL-422 ok). Prints only
PASS/FAIL + non-secret evidence (ids, booleans, the rendered block text). No DB URL / secret echoed.
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

# Bogus, clearly-marked test tenant — deleted at the end (DSR cascade). Never a real number/tenant.
_TID = str(uuid4())
_OP = str(uuid4())  # synthetic operator id (provenance)
_AGENT = "manager"
_KEY = "strategy:winback_dev_probe"
_DIRECTIVE = "Prioritise dormant high-value customers this week; keep the tone warm and concise."


def _init_pool() -> None:
    """Point orchestrator.graph._pool at the injected dev DB (TEAM_SUPABASE_DB_URL / DATABASE_URL).
    The URL is read from env into the pool config; it is NEVER printed."""
    dsn = os.environ.get("TEAM_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("FAIL: no TEAM_SUPABASE_DB_URL / DATABASE_URL in env (run via `railway run`)")
        sys.exit(2)
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=2,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )


def main() -> int:
    _init_pool()
    from orchestrator import graph as graph_mod
    from orchestrator.agents.agent_memory import get_active_memory, upsert_directive

    pool = graph_mod.get_pool()
    ok = True

    # Seed a bogus test tenant.
    with pool.connection() as c:
        c.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'paid_active')",
            (_TID, f"vt556-devprobe-{_TID[:8]}"),
        )
    print(f"[setup] bogus test tenant {_TID} created")

    try:
        # 1. ingest the directive (store path the API calls).
        version = upsert_directive(
            _TID, memory_key=_KEY, content=_DIRECTIVE,
            authored_by_operator_id=_OP, agent=_AGENT, authority="vtr",
        )
        print(f"[step1] upsert_directive OK version={version}")

        # 2. persisted with provenance + retrieval_eligible.
        with pool.connection() as c:
            row = c.execute(
                "SELECT authority, authored_by_operator_id, retrieval_eligible, source, content "
                "FROM agent_memory WHERE tenant_id = %s AND memory_key = %s",
                (_TID, _KEY),
            ).fetchone()
        prov_ok = (
            row is not None
            and row["authority"] == "vtr"
            and str(row["authored_by_operator_id"]) == _OP
            and row["retrieval_eligible"] is True
            and row["source"] == "learned"
        )
        pii_ok = "9876543210" not in (row["content"] if row else "")  # (no PII in this directive)
        print(f"[step2] provenance+retrieval_eligible={prov_ok} pii_redacted_shape={pii_ok}")
        ok = ok and prov_ok

        # 3. get_active_memory returns it (retrieval works on live dev).
        active = get_active_memory(_TID, agent=_AGENT)
        hit = next((e for e in active if e["memory_key"] == _KEY), None)
        retr_ok = hit is not None and hit["authority"] == "vtr" and "dormant" in hit["content"]
        print(f"[step3] get_active_memory returned the directive={retr_ok} (n_active={len(active)})")
        ok = ok and retr_ok

        # 4. the manager directive block renders (MANAGER_MEMORY_RETRIEVAL flag ON on dev).
        from uuid import UUID

        from orchestrator.agent.dispatch import (
            _build_manager_directive_block,
            _manager_memory_retrieval_enabled,
        )

        flag = _manager_memory_retrieval_enabled()
        block = _build_manager_directive_block(UUID(_TID))
        block_ok = flag and block is not None and "## VTR directives" in block and "[VTR]" in block
        print(f"[step4] MANAGER_MEMORY_RETRIEVAL={flag} directive_block_renders={block_ok}")
        if block:
            print("----- rendered block -----")
            print(block)
            print("--------------------------")
        ok = ok and block_ok

    finally:
        with pool.connection() as c:
            c.execute("DELETE FROM tenants WHERE id = %s", (_TID,))  # DSR cascade cleanup
        print(f"[cleanup] test tenant {_TID} deleted")

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} — teach→retrieval+block-render proven on live dev; "
          "the live-dispatch-consumes-it leg needs an inbound trigger (flagged gap).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
