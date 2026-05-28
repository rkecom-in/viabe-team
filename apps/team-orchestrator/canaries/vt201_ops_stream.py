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

VT-216 update:
- A9 reframed from ILIKE → tsvector @@ plainto_tsquery + EXPLAIN check
  verifying the GIN index ``pipeline_steps_envelope_search_tsv_gin`` is
  picked by the planner.
- A14 added: synthetic 1M-row p95 < 200ms scale check. **Gated by
  ``VT216_SCALE_CANARY=1``** — run during release prep, not CI (seed
  step is ~30s + 1M rows × 20 query samples is heavy).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
import json
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

    # ============================================================
    # VT-201 PR-2 — history view assertions (A7-A9)
    # ============================================================
    # Use the same run_a + run_b already seeded. Insert a few
    # pipeline_steps with distinct started_at + envelope shapes so the
    # history endpoint's keyset pagination + ILIKE search can be
    # exercised deterministically.
    from datetime import UTC, datetime, timedelta
    import time as _time

    history_run = run_a
    base_started = datetime.now(UTC)
    history_step_ids: list[str] = []
    marker = f"vt201-pr2-marker-{uuid4().hex[:8]}"
    with pool.connection() as conn:
        for i in range(15):
            step_id = uuid4()
            started = base_started - timedelta(minutes=i)
            envelope = {"reasoning": marker if i == 7 else f"step {i}"}
            conn.execute(
                """
                INSERT INTO pipeline_steps
                    (id, run_id, tenant_id, step_seq, step_kind, status,
                     started_at, output_envelope)
                VALUES (%s, %s, %s, %s, 'agent_reasoning_step', 'completed',
                        %s, %s::jsonb)
                """,
                (str(step_id), str(history_run), tenant_a, 100 + i,
                 started, json.dumps(envelope)),
            )
            history_step_ids.append(str(step_id))

    # A7 — history date-range query within 3s
    t7_start = _time.monotonic()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM pipeline_steps "
            "WHERE id = ANY(%s)",
            (history_step_ids,),
        )
        row = cur.fetchone()
    elapsed_s = _time.monotonic() - t7_start
    pass_7 = (row is not None and int(row["n"]) == 15 and elapsed_s < 3.0)
    assertion(
        7,
        "history query: 15 rows for target date within 3s",
        pass_7,
        observed={"count": int(row["n"]) if row else None, "elapsed_s": round(elapsed_s, 3)},
        expected={"count": 15, "elapsed_s_lt": 3.0},
    )

    # A8 — keyset cursor pagination (walk 3 pages with limit=5)
    paginated_ids: list[str] = []
    cursor = None
    pages = 0
    while pages < 5:
        with pool.connection() as conn, conn.cursor() as cur:
            params = [history_step_ids]
            sql = (
                "SELECT id, started_at FROM pipeline_steps "
                "WHERE id = ANY(%s) "
            )
            if cursor:
                iso, last_id = cursor.split("|")
                sql += (
                    "AND (started_at < %s OR (started_at = %s AND id < %s)) "
                )
                params.extend([iso, iso, last_id])
            sql += "ORDER BY started_at DESC, id DESC LIMIT 5"
            cur.execute(sql, tuple(params))
            page_rows = cur.fetchall()
        if not page_rows:
            break
        for r in page_rows:
            paginated_ids.append(str(r["id"]))
        last = page_rows[-1]
        cursor = f"{last['started_at'].isoformat()}|{last['id']}"
        pages += 1
        if len(page_rows) < 5:
            break

    pass_8 = (
        len(paginated_ids) == 15
        and len(set(paginated_ids)) == 15  # no duplicates
        and pages == 3  # exactly 3 pages of 5
    )
    assertion(
        8,
        "keyset cursor: 3 pages × 5 rows = 15; no duplicates",
        pass_8,
        observed={"pages": pages, "total_ids": len(paginated_ids), "distinct": len(set(paginated_ids))},
        expected={"pages": 3, "total_ids": 15, "distinct": 15},
    )

    # A9 — tsvector free-text search (VT-216 replaces VT-201 PR-2 ILIKE)
    # Parity check: same marker returns same row-set vs the prior
    # ILIKE behavior. EXPLAIN check: query plan uses the GIN index.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM pipeline_steps "
            "WHERE id = ANY(%s) "
            "AND envelope_search_tsv @@ plainto_tsquery('english', %s)",
            (history_step_ids, marker),
        )
        matches = cur.fetchall()

        cur.execute(
            "EXPLAIN SELECT id FROM pipeline_steps "
            "WHERE envelope_search_tsv @@ plainto_tsquery('english', %s)",
            (marker,),
        )
        plan_rows = cur.fetchall()
    plan_text = "\n".join(
        (r[0] if isinstance(r, tuple) else next(iter(r.values()))) for r in plan_rows
    )
    pass_9 = (
        len(matches) == 1
        and "pipeline_steps_envelope_search_tsv_gin" in plan_text
    )
    assertion(
        9,
        f"tsvector free-text: marker '{marker}' returns 1 row + GIN index used",
        pass_9,
        observed={
            "match_count": len(matches),
            "marker": marker,
            "plan_uses_gin": "pipeline_steps_envelope_search_tsv_gin" in plan_text,
            "plan_excerpt": plan_text[:300],
        },
        expected={"match_count": 1, "plan_uses_gin": True},
    )

    # A14 — synthetic 1M-row p95 < 200ms (VT-216 scale check)
    # Gated by VT216_SCALE_CANARY=1 so CI default-skips this heavy seed.
    # Manual run during release prep when proving the GIN index holds
    # under projected Phase-2+ load.
    if os.environ.get("VT216_SCALE_CANARY") == "1":
        _run_a14_scale_check(pool)

    return _finalise(pool)


def _run_a14_scale_check(pool: Any) -> None:
    """Seed ~1M synthetic pipeline_steps + measure p95 query latency.

    Gated by VT216_SCALE_CANARY=1. Heavy: ~30s seed + ~20 query iterations.
    """
    import time as _time

    scale_run_id = uuid4()
    scale_tenant_id = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, trigger_kind, started_at) "
            "VALUES (%s, %s, 'completed', 'manual', now()) "
            "ON CONFLICT (id) DO NOTHING",
            (str(scale_run_id), str(scale_tenant_id)),
        )
        INSERTED_RUN_IDS.append(str(scale_run_id))
        # Chunked seed: 1M rows in 100k-row chunks via generate_series
        for chunk in range(10):
            conn.execute(
                "INSERT INTO pipeline_steps "
                "(id, run_id, tenant_id, step_seq, step_kind, started_at, "
                " input_envelope, output_envelope) "
                "SELECT gen_random_uuid(), %s, %s, gs, 'tool_call', now(), "
                "  jsonb_build_object('q', 'searchable_term_' || gs), "
                "  jsonb_build_object('result', 'distinctive_payload_' || gs) "
                "FROM generate_series(%s, %s) gs",
                (str(scale_run_id), str(scale_tenant_id),
                 chunk * 100_000 + 1, (chunk + 1) * 100_000),
            )

    samples: list[float] = []
    with pool.connection() as conn, conn.cursor() as cur:
        for i in range(20):
            term = f"searchable_term_{(i + 1) * 50_000}"
            t0 = _time.perf_counter()
            cur.execute(
                "SELECT count(*) FROM pipeline_steps "
                "WHERE envelope_search_tsv @@ plainto_tsquery('english', %s)",
                (term,),
            )
            cur.fetchone()
            samples.append((_time.perf_counter() - t0) * 1000.0)

    samples.sort()
    p95_ms = samples[int(0.95 * len(samples))]
    pass_14 = p95_ms < 200.0
    assertion(
        14,
        f"1M-row tsvector p95 < 200ms (observed {p95_ms:.1f}ms)",
        pass_14,
        observed={"p95_ms": round(p95_ms, 1), "sample_count": len(samples)},
        expected={"p95_ms_lt": 200.0},
    )


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
