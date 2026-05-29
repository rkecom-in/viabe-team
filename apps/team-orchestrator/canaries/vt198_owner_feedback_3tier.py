#!/usr/bin/env python3
"""VT-198 owner feedback 3-tier canary (Rule #15, DR-15).

Eight assertions:

- A1: implicit_attribution writes thumbs_up when synthetic completed
  campaign + attribution_outcome > attribution_baseline
- A2: emoji_reaction_handler maps 👍 → thumbs_up, gates on owner_inputs
- A3: dashboard_review_writer accepts valid signal + writes row
- A4: source_metadata JSON contains NO PII (E.164, phone_tok_, @-emails)
- A5: RLS SELECT — tenant A cannot SELECT tenant B's rows
- A6 (LOCK 1): "Thanks 👍" → is_emoji_only_body returns False, NOT routed
- A7 (LOCK 2): implicit_attribution running twice → exactly 1 implicit row
- A8 (LOCK 3): INSERT with tenant_id != current_tenant → constraint violation

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt198_owner_feedback_3tier.py
    )

Wall-clock budget ≤ 20s. Cost: 0 paise.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []
INSERTED_RUNS: list[str] = []


def assertion(
    num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None
) -> None:
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
    print("PREFLIGHT OK")


def _seed_tenant(pool: Any, owner_inputs: bool = True) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number, owner_inputs) "
            "VALUES (%s, %s, 'standard', 'trial', %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tid, f"vt198-{tid[:8]}", f"+9199{uuid4().hex[:8]}", owner_inputs),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _seed_completed_run(pool: Any, tenant_id: str, outcome: float, baseline: float) -> str:
    rid = str(uuid4())
    import json as _json
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs
                (id, tenant_id, status, trigger_kind, started_at, completed_at, terminal_state_metadata)
            VALUES (%s, %s, 'completed', 'manual', now() - interval '1 day', now(), %s::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            (rid, tenant_id, _json.dumps({
                "attribution_outcome": outcome,
                "attribution_baseline": baseline,
            })),
        )
    INSERTED_RUNS.append(rid)
    return rid


def _cleanup(pool: Any) -> None:
    if not INSERTED_TENANTS:
        return
    with pool.connection() as conn:
        for tid in INSERTED_TENANTS:
            conn.execute("DELETE FROM owner_feedback WHERE tenant_id = %s", (tid,))
            conn.execute("DELETE FROM pipeline_runs WHERE tenant_id = %s", (tid,))
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    from uuid import UUID
    from orchestrator.feedback import (
        handle_emoji_reaction,
        is_emoji_only_body,
        run_implicit_attribution_sweep,
        write_dashboard_feedback,
    )

    # --- A1: implicit_attribution thumbs_up ---
    tid_a1 = _seed_tenant(pool)
    rid_a1 = _seed_completed_run(pool, tid_a1, outcome=100, baseline=50)
    sweep_result = run_implicit_attribution_sweep()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT signal FROM owner_feedback "
            "WHERE tenant_id = %s AND run_id = %s AND tier = 'implicit'",
            (tid_a1, rid_a1),
        )
        row = cur.fetchone()
    sig = row["signal"] if isinstance(row, dict) else (row[0] if row else None)
    pass_1 = sig == "thumbs_up"
    assertion(1, "implicit_attribution writes thumbs_up for outcome > baseline",
              pass_1, observed={"signal": sig, "sweep_counts": sweep_result})

    # --- A2: emoji_reaction_handler ---
    tid_a2 = _seed_tenant(pool, owner_inputs=True)
    rid_a2 = str(uuid4())
    r2 = handle_emoji_reaction(tenant_id=UUID(tid_a2), run_id=UUID(rid_a2), body="👍")
    pass_2 = r2.get("status") == "written" and r2.get("signal") == "thumbs_up"
    assertion(2, "emoji 👍 → thumbs_up written", pass_2, observed=r2)

    # --- A3: dashboard_review_writer ---
    tid_a3 = _seed_tenant(pool)
    rid_a3 = str(uuid4())
    r3 = write_dashboard_feedback(
        tenant_id=UUID(tid_a3), run_id=UUID(rid_a3),
        signal="thumbs_down", reason="Wrong segment targeted",
    )
    pass_3 = r3.get("status") == "written"
    assertion(3, "dashboard_review_writer accepts valid signal + writes row",
              pass_3, observed=r3)

    # --- A4: source_metadata contains NO PII ---
    pii_pattern = re.compile(r"(\+?91\d{9,12}|phone_tok_[0-9a-f]+|\b[\w.+-]+@[\w.-]+\.\w+)")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_metadata::text AS m FROM owner_feedback "
            "WHERE tenant_id = ANY(%s)",
            (INSERTED_TENANTS,),
        )
        all_meta = [r["m"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    leaks = [m for m in all_meta if pii_pattern.search(m)]
    pass_4 = len(leaks) == 0
    assertion(4, "source_metadata contains zero PII",
              pass_4, observed={"row_count": len(all_meta), "leaks": leaks})

    # --- A5: RLS SELECT isolation ---
    tid_a5 = _seed_tenant(pool)
    rid_a5 = str(uuid4())
    write_dashboard_feedback(
        tenant_id=UUID(tid_a5), run_id=UUID(rid_a5), signal="thumbs_up",
    )
    # Read as tid_a1's tenant — should see none of tid_a5's rows
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL app.current_tenant = %s", (tid_a1,))
        cur.execute(
            "SELECT count(*) AS n FROM owner_feedback WHERE tenant_id = %s",
            (tid_a5,),
        )
        rls_row = cur.fetchone()
    rls_count = int(rls_row["n"] if isinstance(rls_row, dict) else rls_row[0])
    pass_5 = rls_count == 0
    assertion(5, "RLS SELECT: tenant A cannot read tenant B's rows",
              pass_5, observed={"count_seen": rls_count})

    # --- A6 (LOCK 1): natural-language with emoji → NOT routed ---
    pass_6 = (
        is_emoji_only_body("Thanks 👍") is False
        and is_emoji_only_body("👍") is True
        and is_emoji_only_body("👍 👏") is True
        and is_emoji_only_body("👍 nice") is False
    )
    assertion(6, "is_emoji_only_body distinguishes structural emoji-only vs mixed",
              pass_6, observed={
                  "thanks_thumbs": is_emoji_only_body("Thanks 👍"),
                  "thumbs_only": is_emoji_only_body("👍"),
                  "thumbs_clap": is_emoji_only_body("👍 👏"),
                  "thumbs_nice": is_emoji_only_body("👍 nice"),
              })

    # --- A7 (LOCK 2): implicit sweep idempotent ---
    sweep_2 = run_implicit_attribution_sweep()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM owner_feedback "
            "WHERE tenant_id = %s AND run_id = %s AND tier = 'implicit'",
            (tid_a1, rid_a1),
        )
        idem_row = cur.fetchone()
    idem_count = int(idem_row["n"] if isinstance(idem_row, dict) else idem_row[0])
    pass_7 = idem_count == 1
    assertion(7, "implicit sweep idempotent: re-run produces exactly 1 row",
              pass_7, observed={"final_count": idem_count, "sweep2_counts": sweep_2})

    # --- A8 (LOCK 3): INSERT cross-tenant via RLS WITH CHECK ---
    # Set tenant context to A; attempt INSERT with tenant_id = B → must fail.
    tid_a8_a = _seed_tenant(pool)
    tid_a8_b = _seed_tenant(pool)
    insert_blocked = False
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.current_tenant = %s", (tid_a8_a,))
            # Operator-claim absent → INSERT must be rejected by RLS WITH CHECK
            # when tenant_id doesn't match current_tenant.
            cur.execute(
                "INSERT INTO owner_feedback "
                "(tenant_id, tier, signal, source_metadata) "
                "VALUES (%s, 'dashboard', 'thumbs_up', '{}'::jsonb)",
                (tid_a8_b,),
            )
    except Exception as exc:  # noqa: BLE001
        insert_blocked = "row-level security" in str(exc).lower() or "policy" in str(exc).lower()
    pass_8 = insert_blocked
    assertion(8, "RLS INSERT: tenant A cannot insert row with tenant_id = B",
              pass_8, observed={"blocked": insert_blocked})

    _cleanup(pool)
    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
