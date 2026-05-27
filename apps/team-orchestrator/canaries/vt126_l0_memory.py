#!/usr/bin/env python3
"""VT-126 L0 memory orchestrator-agent integration canary (Rule #15, DR-15).

Subshell-source ONLY ``.viabe/secrets/supabase-dev.env``:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt126_l0_memory.py 2>&1 | tee /tmp/vt126-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Deterministic substrate; ANTHROPIC_API_KEY
ABSENT at PREFLIGHT (defense-in-depth per DR-15).

Wall-clock budget ≤ 30s. Cost budget: 0 paise.

8 assertions matching brief §Canary:

- A1: write new (fragment_type, cohort_key) → row INSERTed with
  observation_count=1; inserted=True returned.
- A2: write existing key → observation_count INCREMENTed by 1
  (UPSERT) and inserted=False; original ``content`` preserved.
- A3: query_l0 returns [] when observation_count < 10 (k-anonymity
  gate at the SQL predicate layer).
- A4: After 10 observations, query_l0 returns the fragment with
  observation_count >= 10.
- A5: Cohort aggregation crosses tenants — fragments aggregate by
  ``cohort_key`` not tenant_id (CL-390 cohort-keyed).
- A6: PII reject — write_l0_fragment with phone-bearing content
  raises PiiInContentError; NO row inserted.
- A7: ``@tool_step`` decoration emits one pipeline_steps row per
  call with step_kind='l0_write' / 'l0_query' (CL-220).
- A8: ANTHROPIC ABSENT preflight (defense-in-depth).
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
INSERTED_FRAGMENT_IDS: list[str] = []
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


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
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt126-canary-salt")

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.decorators import observability_context
    from orchestrator.observability.l0_memory import (
        K_ANONYMITY_THRESHOLD,
        PiiInContentError,
        query_l0,
        write_l0_fragment,
    )

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

    # Each run uses a fresh cohort_key so re-runs are independent (the
    # UNIQUE constraint would otherwise turn re-runs into UPSERT chains).
    cohort_a = f"canary-vt126|tier_2|founding|{uuid4().hex[:8]}"
    cohort_b = f"canary-vt126|tier_3|launch|{uuid4().hex[:8]}"
    pii_cohort = f"canary-vt126|tier_1|onboarding|{uuid4().hex[:8]}"

    # ----------------------------------------------------------------
    # A1: write new → INSERT, observation_count=1, inserted=True
    # ----------------------------------------------------------------
    result1 = write_l0_fragment(
        fragment_type="routing_decision",
        cohort_key=cohort_a,
        content={"choice": "spawn_sales_recovery", "reason": "weekly_cadence"},
    )
    INSERTED_FRAGMENT_IDS.append(result1["fragment_id"])
    pass_1 = (
        result1["observation_count"] == 1
        and result1["inserted"] is True
        and result1["fragment_id"]
    )
    assertion(
        1,
        "write_l0_fragment new key → INSERT observation_count=1 inserted=True",
        pass_1,
        observed={
            "observation_count": result1["observation_count"],
            "inserted": result1["inserted"],
            "fragment_id_set": bool(result1["fragment_id"]),
        },
        expected={"observation_count": 1, "inserted": True},
    )

    # ----------------------------------------------------------------
    # A2: write existing key → UPSERT, observation_count++, inserted=False
    # ----------------------------------------------------------------
    result2 = write_l0_fragment(
        fragment_type="routing_decision",
        cohort_key=cohort_a,
        content={"choice": "respond_direct", "reason": "urgency_signal"},
    )
    pass_2 = (
        result2["fragment_id"] == result1["fragment_id"]
        and result2["observation_count"] == 2
        and result2["inserted"] is False
    )
    # Original content preserved (per write_l0_fragment docstring).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM l0_fragments WHERE id = %s",
            (result1["fragment_id"],),
        )
        row = cur.fetchone()
    content_preserved = (
        row is not None
        and isinstance(row["content"], dict)
        and row["content"].get("choice") == "spawn_sales_recovery"
    )
    pass_2 = pass_2 and content_preserved
    assertion(
        2,
        "write_l0_fragment existing key → UPSERT inserted=False obs++ content preserved",
        pass_2,
        observed={
            "fragment_id_matches": result2["fragment_id"] == result1["fragment_id"],
            "observation_count": result2["observation_count"],
            "inserted": result2["inserted"],
            "stored_choice": row["content"].get("choice") if row else None,
        },
        expected={
            "observation_count": 2,
            "inserted": False,
            "stored_choice": "spawn_sales_recovery",
        },
    )

    # ----------------------------------------------------------------
    # A3: query_l0 under k → returns []
    # ----------------------------------------------------------------
    q_below = query_l0(fragment_type="routing_decision", cohort_key=cohort_a)
    pass_3 = q_below["matched_count"] == 0 and q_below["fragments"] == []
    assertion(
        3,
        "query_l0 below k=10 → empty (k-anonymity SQL predicate gate)",
        pass_3,
        observed={
            "matched_count": q_below["matched_count"],
            "fragments_len": len(q_below["fragments"]),
            "current_observation_count": result2["observation_count"],
        },
        expected={"matched_count": 0, "fragments_len": 0},
    )

    # ----------------------------------------------------------------
    # A4: push to k=10 via additional writes → query returns the fragment
    # ----------------------------------------------------------------
    for _ in range(K_ANONYMITY_THRESHOLD - 2):  # already at 2; need 8 more.
        write_l0_fragment(
            fragment_type="routing_decision",
            cohort_key=cohort_a,
            content={"choice": "respond_direct", "reason": "spam"},
        )
    q_at = query_l0(fragment_type="routing_decision", cohort_key=cohort_a)
    pass_4 = (
        q_at["matched_count"] == 1
        and len(q_at["fragments"]) == 1
        and q_at["fragments"][0]["observation_count"] >= K_ANONYMITY_THRESHOLD
    )
    assertion(
        4,
        "query_l0 at k=10 → returns fragment (k-anonymity threshold reached)",
        pass_4,
        observed={
            "matched_count": q_at["matched_count"],
            "first_obs_count": (
                q_at["fragments"][0]["observation_count"] if q_at["fragments"] else None
            ),
        },
        expected={"matched_count": 1, "first_obs_count_gte": K_ANONYMITY_THRESHOLD},
    )

    # ----------------------------------------------------------------
    # A5: cohort aggregation crosses tenants (cohort-keyed, CL-390)
    # ----------------------------------------------------------------
    # Two distinct synthetic tenants observe a NEW cohort_b; the
    # observation_count is shared because L0 is cohort-keyed.
    tenant_x = uuid4()
    tenant_y = uuid4()
    INSERTED_TENANT_IDS.extend([str(tenant_x), str(tenant_y)])
    write_l0_fragment(
        fragment_type="specialist_outcome",
        cohort_key=cohort_b,
        content={"specialist": "sales_recovery", "result": "approved"},
    )
    result5 = write_l0_fragment(
        fragment_type="specialist_outcome",
        cohort_key=cohort_b,
        content={"specialist": "sales_recovery", "result": "rejected"},
    )
    # observation_count = 2 from two writes (no tenant_id column to gate
    # on); the cohort_key alone keys the row.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.l0_fragments') IS NOT NULL AS table_exists, "
            "EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='l0_fragments' AND column_name='tenant_id') AS has_tenant_col"
        )
        info = cur.fetchone()
    pass_5 = (
        result5["observation_count"] == 2
        and info is not None
        and info["table_exists"] is True
        and info["has_tenant_col"] is False
    )
    INSERTED_FRAGMENT_IDS.append(result5["fragment_id"])
    assertion(
        5,
        "L0 cohort-keyed (NOT tenant-keyed): aggregation crosses tenants",
        pass_5,
        observed={
            "observation_count_after_2_writes": result5["observation_count"],
            "table_exists": info["table_exists"] if info else None,
            "has_tenant_id_column": info["has_tenant_col"] if info else None,
        },
        expected={
            "observation_count_after_2_writes": 2,
            "has_tenant_id_column": False,
        },
    )

    # ----------------------------------------------------------------
    # A6: PII reject — phone-bearing content → PiiInContentError
    # ----------------------------------------------------------------
    pii_raised = False
    pii_error_msg: str | None = None
    try:
        write_l0_fragment(
            fragment_type="trigger_pattern",
            cohort_key=pii_cohort,
            content={"customer_note": "owner asked me to call +919876543210 back"},
        )
    except PiiInContentError as exc:
        pii_raised = True
        pii_error_msg = str(exc)
    # Verify NO row was inserted for the PII cohort.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM l0_fragments WHERE cohort_key = %s",
            (pii_cohort,),
        )
        n_rows = cur.fetchone()
    pii_no_row = n_rows is not None and int(n_rows["n"]) == 0
    pass_6 = pii_raised and pii_no_row
    assertion(
        6,
        "PII reject: phone in content raises PiiInContentError, NO row inserted",
        pass_6,
        observed={
            "pii_raised": pii_raised,
            "error_msg_prefix": (pii_error_msg or "")[:120],
            "rows_in_pii_cohort": (int(n_rows["n"]) if n_rows else None),
        },
        expected={"pii_raised": True, "rows_in_pii_cohort": 0},
    )

    # ----------------------------------------------------------------
    # A7: @tool_step emits pipeline_steps rows with step_kind='l0_write'
    # / 'l0_query' — exercised through orchestrator-agent's exported
    # ``write_l0_fragment`` / ``query_l0`` tools under an
    # ObservabilityContext.
    # ----------------------------------------------------------------
    from orchestrator.agent.orchestrator_agent import (
        query_l0 as q_tool,
        write_l0_fragment as w_tool,
    )

    obs_run_id = uuid4()
    obs_tenant_id = uuid4()
    INSERTED_RUN_IDS.append(str(obs_run_id))
    INSERTED_TENANT_IDS.append(str(obs_tenant_id))
    # Insert the tenant row so any RLS / FK pathways referenced by
    # write_step remain coherent (write_step itself is tenant-agnostic
    # but the canary tries to mimic real-call shape).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(obs_tenant_id), f"canary-vt126-{obs_tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running') ON CONFLICT (id) DO NOTHING",
            (str(obs_run_id), str(obs_tenant_id)),
        )

    tool_cohort = f"canary-vt126-tool|tier_2|founding|{uuid4().hex[:8]}"
    with observability_context(run_id=obs_run_id, tenant_id=obs_tenant_id):
        # langchain @tool tools expose .invoke(input_dict). The wrapped
        # impl underneath is the @tool_step-decorated function; we
        # exercise it through the langchain tool surface to mirror
        # production call flow.
        w_tool.invoke(
            {
                "fragment_type": "routing_decision",
                "cohort_key": tool_cohort,
                "content": {"choice": "respond_direct"},
            }
        )
        q_tool.invoke(
            {"fragment_type": "routing_decision", "cohort_key": tool_cohort, "k": 5}
        )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name FROM pipeline_steps "
            "WHERE run_id = %s ORDER BY step_seq",
            (str(obs_run_id),),
        )
        step_rows = cur.fetchall()
    step_kinds = [r["step_kind"] for r in step_rows]
    step_names = [r["step_name"] for r in step_rows]
    pass_7 = (
        "l0_write" in step_kinds
        and "l0_query" in step_kinds
        and "write_l0_fragment" in step_names
        and "query_l0" in step_names
    )
    assertion(
        7,
        "@tool_step emits pipeline_steps rows: step_kind='l0_write' + 'l0_query'",
        pass_7,
        observed={"step_kinds": step_kinds, "step_names": step_names},
        expected={
            "step_kinds_includes": ["l0_write", "l0_query"],
            "step_names_includes": ["write_l0_fragment", "query_l0"],
        },
    )

    # ----------------------------------------------------------------
    # A8: ANTHROPIC ABSENT (defense-in-depth)
    # ----------------------------------------------------------------
    pass_8 = not os.environ.get("ANTHROPIC_API_KEY")
    assertion(
        8,
        "ANTHROPIC_API_KEY absent throughout (defense-in-depth DR-15)",
        pass_8,
        observed={"ANTHROPIC_API_KEY": "<absent>" if pass_8 else "<PRESENT — FAIL>"},
        expected={"ANTHROPIC_API_KEY": "<absent>"},
    )

    return _finalise(pool)


def _finalise(pool: Any) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (deterministic substrate; no LLM) ===")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_FRAGMENT_IDS:
                cur.execute(
                    "DELETE FROM l0_fragments WHERE id = ANY(%s)",
                    (INSERTED_FRAGMENT_IDS,),
                )
            cur.execute(
                "DELETE FROM l0_fragments WHERE cohort_key LIKE 'canary-vt126%%'"
            )
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
                    "DELETE FROM tenants WHERE id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
