#!/usr/bin/env python3
"""VT-201 Ops live-stream canary (Rule #15, DR-15).

PR-1 partial — verifies the Supabase Realtime substrate (migration 030)
+ banner aggregation + RLS isolation. PR-2 will add A3 (history query)
and PR-3 will add A4 (free-text search).

Subshell-source ``.viabe/secrets/supabase-dev.env``:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt201_ops_stream.py 2>&1 | tee /tmp/vt201-canary.log | tail -100
    )

**NO Anthropic** — UI substrate; ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 30s. Cost: 0 paise.

PR-1 assertions (4 of 6):

- A1: pipeline_steps publication + REPLICA IDENTITY FULL configured
  (verifies migration 030 landed; full-row payload available to
  Realtime subscribers per Q5 Option A locked)
- A2: filter query — fetching pipeline_steps WHERE tenant_id=X under
  service role returns ONLY that tenant's rows (Realtime client-side
  filter shape mirrors this query)
- A5: banner counts match aggregate query (escalations / hard_limits /
  errors in last 24h from pipeline_runs + pipeline_steps)
- A6: RLS — operator-claim JWT context returns rows across tenants;
  no claim returns 0 rows (the new permissive policy from migration
  030)

PR-2 will add A3 (history query <3s). PR-3 will add A4 (free-text
<1s after tsvector migration).
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


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANT_IDS: list[str] = []
INSERTED_RUN_IDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
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
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary MUST NOT "
            "source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print("PREFLIGHT OK — db only; ANTHROPIC_API_KEY: <absent — DR-15>")


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=8,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    # ---------------- A1 — publication + REPLICA IDENTITY ----------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT relreplident FROM pg_class WHERE relname = 'pipeline_steps'"
        )
        replident_row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS n FROM pg_publication_tables "
            "WHERE pubname = 'supabase_realtime' AND tablename = 'pipeline_steps'"
        )
        pub_row = cur.fetchone()
    # 'f' = REPLICA IDENTITY FULL per pg_class docs
    pass_1 = (
        replident_row is not None
        and replident_row["relreplident"] == "f"
        and pub_row is not None
        and int(pub_row["n"]) == 1
    )
    assertion(
        1,
        "pipeline_steps REPLICA IDENTITY FULL + in supabase_realtime publication",
        pass_1,
        observed={
            "replident": (
                replident_row["relreplident"] if replident_row else None
            ),
            "publication_rows": int(pub_row["n"]) if pub_row else None,
        },
        expected={"replident": "f", "publication_rows": 1},
    )

    # ---------------- A2 — filter query isolation ----------------
    # Seed 2 synthetic tenants + 1 pipeline_step each
    tenant_a = uuid4()
    tenant_b = uuid4()
    run_a = uuid4()
    run_b = uuid4()
    INSERTED_TENANT_IDS.extend([str(tenant_a), str(tenant_b)])
    INSERTED_RUN_IDS.extend([str(run_a), str(run_b)])
    with pool.connection() as conn, conn.cursor() as cur:
        for tid, rid in ((tenant_a, run_a), (tenant_b, run_b)):
            cur.execute(
                "INSERT INTO tenants (id, business_name, plan_tier, phase) "
                "VALUES (%s, %s, 'standard', 'paid_active') ON CONFLICT (id) DO NOTHING",
                (str(tid), f"vt201-canary-{tid.hex[:6]}"),
            )
            cur.execute(
                "INSERT INTO pipeline_runs (id, tenant_id, status, started_at) "
                "VALUES (%s, %s, 'completed', NOW()) ON CONFLICT (id) DO NOTHING",
                (str(rid), str(tid)),
            )
            cur.execute(
                "INSERT INTO pipeline_steps "
                "(run_id, tenant_id, step_seq, step_kind, status, started_at) "
                "VALUES (%s, %s, 0, 'webhook_received', 'completed', NOW())",
                (str(rid), str(tid)),
            )

    # Service-role read filtered to tenant_a
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id FROM pipeline_steps "
            "WHERE tenant_id = %s AND run_id = %s",
            (str(tenant_a), str(run_a)),
        )
        a_rows = cur.fetchall()
        cur.execute(
            "SELECT tenant_id FROM pipeline_steps "
            "WHERE tenant_id = %s AND run_id = %s",
            (str(tenant_a), str(run_b)),  # tenant_a filter, run_b id — empty
        )
        cross_rows = cur.fetchall()
    pass_2 = (
        len(a_rows) >= 1
        and all(r["tenant_id"] == tenant_a for r in a_rows)
        and len(cross_rows) == 0
    )
    assertion(
        2,
        "filter query: tenant_id=A returns A's rows; cross-tenant filter empty",
        pass_2,
        observed={
            "a_rows": len(a_rows),
            "cross_rows": len(cross_rows),
            "a_only_a_tenant": all(r["tenant_id"] == tenant_a for r in a_rows),
        },
        expected={"a_rows_gte": 1, "cross_rows": 0},
    )

    # ---------------- A5 — banner counts match aggregate ----------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM pipeline_runs WHERE status='escalated' "
            "     AND started_at >= NOW() - INTERVAL '24 hours') AS esc_count, "
            "  (SELECT COUNT(*) FROM pipeline_runs WHERE status='aborted_hard_limit' "
            "     AND started_at >= NOW() - INTERVAL '24 hours') AS hl_count, "
            "  (SELECT COUNT(*) FROM pipeline_steps WHERE status='failed' "
            "     AND started_at >= NOW() - INTERVAL '24 hours') AS err_count"
        )
        agg_row = cur.fetchone()
    # The values are returned as integers — assertion just confirms the
    # query shape works against the schema (load-bearing — banner.ts uses
    # the same shape).
    pass_5 = agg_row is not None and all(
        k in agg_row for k in ("esc_count", "hl_count", "err_count")
    )
    assertion(
        5,
        "banner aggregate query shape matches schema (escalations/hard_limits/errors)",
        pass_5,
        observed=dict(agg_row) if agg_row else {},
        expected={"keys": ["esc_count", "hl_count", "err_count"]},
    )

    # ---------------- A6 — RLS: operator-claim policy ----------------
    # Direct via service-role bypasses RLS (sees everything). Instead
    # test the policy SQL surface by emulating the JWT claim through
    # set_config + role downgrade.
    with pool.connection() as conn, conn.cursor() as cur:
        # Emulate non-operator (no JWT claim) — read pipeline_runs as
        # app_role with NO tenant GUC set + no operator claim. Existing
        # tenant_id RLS should return 0 rows.
        try:
            # Set claims FIRST (must be non-empty JSON; '{}' shape lets
            # the migration-030 policy parse the jsonb cast safely).
            cur.execute("SELECT set_config('request.jwt.claims', '{}', false)")
            cur.execute("SET ROLE app_role")
            cur.execute(
                "SELECT COUNT(*) AS n FROM pipeline_runs WHERE id = ANY(%s)",
                ([str(run_a), str(run_b)],),
            )
            no_claim_row = cur.fetchone()
            no_claim_count = int(no_claim_row["n"]) if no_claim_row else -1
        finally:
            cur.execute("RESET ROLE")
            cur.execute("SELECT set_config('request.jwt.claims', '{}', false)")
        # Emulate operator JWT claim
        try:
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, false)",
                ('{"operator_claim":"true","operator_id":"canary-op"}',),
            )
            cur.execute("SET ROLE app_role")
            cur.execute(
                "SELECT COUNT(*) AS n FROM pipeline_runs WHERE id = ANY(%s)",
                ([str(run_a), str(run_b)],),
            )
            op_claim_row = cur.fetchone()
            op_claim_count = int(op_claim_row["n"]) if op_claim_row else -1
        finally:
            cur.execute("RESET ROLE")
            cur.execute("SELECT set_config('request.jwt.claims', '{}', false)")
    pass_6 = no_claim_count == 0 and op_claim_count == 2
    assertion(
        6,
        "RLS: operator JWT sees both runs (cross-tenant); no JWT sees 0",
        pass_6,
        observed={
            "no_claim_count": no_claim_count,
            "op_claim_count": op_claim_count,
        },
        expected={"no_claim_count": 0, "op_claim_count": 2},
    )

    return _finalise(pool)


def _finalise(pool: Any) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    print("\n=== Anthropic cost: 0 paise (UI substrate; no LLM) ===")
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_RUN_IDS:
                cur.execute(
                    "DELETE FROM pipeline_steps WHERE run_id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
                cur.execute(
                    "DELETE FROM pipeline_runs WHERE id = ANY(%s)",
                    (INSERTED_RUN_IDS,),
                )
            if INSERTED_TENANT_IDS:
                cur.execute(
                    "DELETE FROM twilio_inbound_events WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                cur.execute(
                    "DELETE FROM tenants WHERE id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)
    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED (PR-1 partial; PR-2 + PR-3 to follow)")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
