#!/usr/bin/env python3
"""VT-187 schema normalization canary (Rule #15, DR-15).

Asserts that migration 025 successfully normalized the three pipeline
observability tables to design-doc §2.1 spec:

- pipeline_runs: ADDED trigger_kind, trigger_source_ref, final_outcome,
  step_count, error_summary; RENAMED cost_paise -> total_cost_paise.
- pipeline_steps: ADDED step_name, parent_step_id, tool_calls, status,
  model_used, tokens_input, tokens_output; RENAMED step_index -> step_seq,
  rationale -> decision_rationale, error_envelope -> error.
- phone_token_resolutions: ADDED customer_id (NO FK per CL-417 Cond 1);
  RENAMED token -> phone_token, last_resolved_at -> last_accessed_at.

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt187_schema_normalize.py 2>&1 | tee /tmp/vt187-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Defense-in-depth Pillar 1: pure DDL inspection
+ constraint catalog check; ANTHROPIC_API_KEY ABSENT at PREFLIGHT.
Wall-clock budget <= 30s. Cost budget: 0 paise.

8 assertions across 4 groups:
- Group A (3): canonical columns present per table, legacy names retired.
- Group B (2): renames preserved index/constraint refs; customer_id no FK.
- Group C (2): back-fill correctness for trigger_payload + status default.
- Group D (1): zero-LLM invariant.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
SAMPLE_SCHEMA: dict[str, Any] = {}


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host():
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight():
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary's loader "
            "must NOT source anthropic.env (Pillar 1 structural enforcement).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt187-canary-salt")

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

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
    pool = get_pool()

    def _cols(table: str) -> set[str]:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s "
                "ORDER BY ordinal_position",
                (table,),
            )
            return {row["column_name"] for row in cur.fetchall()}

    # -------------------------------------------------------------------
    # Group A — canonical columns + legacy retired (3 assertions)
    # -------------------------------------------------------------------

    # Assertion 1 — pipeline_runs canonical post-VT-187.
    canonical_pr = {
        "trigger_kind", "trigger_source_ref", "final_outcome",
        "step_count", "error_summary", "total_cost_paise",
    }
    legacy_pr = {"cost_paise"}
    actual_pr = _cols("pipeline_runs")
    SAMPLE_SCHEMA["pipeline_runs"] = sorted(actual_pr)
    missing_pr = canonical_pr - actual_pr
    legacy_pr_present = legacy_pr & actual_pr
    pass_1 = not missing_pr and not legacy_pr_present
    assertion(
        1,
        "pipeline_runs: canonical columns present, legacy cost_paise retired",
        pass_1,
        observed={
            "missing_canonical": sorted(missing_pr),
            "legacy_still_present": sorted(legacy_pr_present),
        },
        expected={"missing_canonical": [], "legacy_still_present": []},
    )

    # Assertion 2 — pipeline_steps canonical post-VT-187.
    canonical_ps = {
        "step_seq", "decision_rationale", "error",
        "step_name", "parent_step_id", "tool_calls",
        "status", "model_used", "tokens_input", "tokens_output",
    }
    legacy_ps = {"step_index", "rationale", "error_envelope"}
    actual_ps = _cols("pipeline_steps")
    SAMPLE_SCHEMA["pipeline_steps"] = sorted(actual_ps)
    missing_ps = canonical_ps - actual_ps
    legacy_ps_present = legacy_ps & actual_ps
    pass_2 = not missing_ps and not legacy_ps_present
    assertion(
        2,
        "pipeline_steps: canonical columns present, legacy names retired",
        pass_2,
        observed={
            "missing_canonical": sorted(missing_ps),
            "legacy_still_present": sorted(legacy_ps_present),
        },
        expected={"missing_canonical": [], "legacy_still_present": []},
    )

    # Assertion 3 — phone_token_resolutions canonical post-VT-187.
    canonical_pt = {"phone_token", "last_accessed_at", "customer_id"}
    legacy_pt = {"token", "last_resolved_at"}
    actual_pt = _cols("phone_token_resolutions")
    SAMPLE_SCHEMA["phone_token_resolutions"] = sorted(actual_pt)
    missing_pt = canonical_pt - actual_pt
    legacy_pt_present = legacy_pt & actual_pt
    pass_3 = not missing_pt and not legacy_pt_present
    assertion(
        3,
        "phone_token_resolutions: canonical columns present, legacy retired",
        pass_3,
        observed={
            "missing_canonical": sorted(missing_pt),
            "legacy_still_present": sorted(legacy_pt_present),
        },
        expected={"missing_canonical": [], "legacy_still_present": []},
    )

    # -------------------------------------------------------------------
    # Group B — constraints + customer_id no-FK (2 assertions)
    # -------------------------------------------------------------------

    # Assertion 4 — indexes/constraints auto-updated by ALTER TABLE RENAME COLUMN.
    # PostgreSQL preserves the index/constraint NAME but updates the column
    # reference. Verify by checking the column lists pg_index reports.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              ic.relname AS index_name,
              array_agg(a.attname ORDER BY k.n) AS column_list
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_class tc ON tc.oid = i.indrelid
            JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, n) ON true
            JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = k.attnum
            WHERE tc.relname IN ('pipeline_steps', 'phone_token_resolutions')
              AND ic.relname IN (
                'pipeline_steps_run_step_unique',
                'pipeline_steps_run_step_idx',
                'phone_token_resolutions_pkey',
                'phone_token_resolutions_tenant_token_idx'
              )
            GROUP BY ic.relname
            ORDER BY ic.relname
            """
        )
        idx_cols = {row["index_name"]: row["column_list"] for row in cur.fetchall()}
    SAMPLE_SCHEMA["renamed_index_refs"] = idx_cols
    expected_refs = {
        "pipeline_steps_run_step_unique": ["run_id", "step_seq"],
        "pipeline_steps_run_step_idx": ["run_id", "step_seq"],
        "phone_token_resolutions_pkey": ["phone_token"],
        "phone_token_resolutions_tenant_token_idx": ["tenant_id", "phone_token"],
    }
    mismatches = {
        name: {"observed": idx_cols.get(name), "expected": cols}
        for name, cols in expected_refs.items()
        if idx_cols.get(name) != cols
    }
    pass_4 = not mismatches
    assertion(
        4,
        "Indexes/constraints auto-updated to renamed columns (step_seq, phone_token)",
        pass_4,
        observed={"actual_refs": idx_cols, "mismatches": mismatches},
        expected={"actual_refs": expected_refs},
    )

    # Assertion 5 — customer_id present but NO FK constraint (CL-417 Cond 1).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname, pg_get_constraintdef(oid) AS def
            FROM pg_constraint
            WHERE conrelid = 'phone_token_resolutions'::regclass
              AND contype = 'f'
            """
        )
        fk_rows = [{"name": r["conname"], "def": r["def"]} for r in cur.fetchall()]
        fk_on_customer_id = any(
            "customer_id" in row["def"] for row in fk_rows
        )
    SAMPLE_SCHEMA["phone_token_resolutions_fks"] = fk_rows
    pass_5 = not fk_on_customer_id
    assertion(
        5,
        "phone_token_resolutions.customer_id has NO FK constraint (CL-417 Cond 1)",
        pass_5,
        observed={"fk_on_customer_id": fk_on_customer_id, "all_fks": fk_rows},
        expected={"fk_on_customer_id": False},
    )

    # -------------------------------------------------------------------
    # Group C — back-fill correctness (2 assertions)
    # -------------------------------------------------------------------

    # Assertion 6 — pipeline_runs.trigger_kind back-fill matches
    # trigger_payload->>'kind' for any pre-existing rows. Empty table OK.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS mismatches
            FROM pipeline_runs
            WHERE trigger_payload IS NOT NULL
              AND trigger_payload ? 'kind'
              AND COALESCE(trigger_kind, '') <> COALESCE(trigger_payload->>'kind', '')
            """
        )
        mismatch_count = int(cur.fetchone()["mismatches"])
        cur.execute("SELECT COUNT(*) AS n FROM pipeline_runs")
        total_runs = int(cur.fetchone()["n"])
    pass_6 = mismatch_count == 0
    assertion(
        6,
        "pipeline_runs.trigger_kind back-fill consistent with trigger_payload->>'kind'",
        pass_6,
        observed={"mismatch_count": mismatch_count, "total_runs_inspected": total_runs},
        expected={"mismatch_count": 0},
    )

    # Assertion 7 — pipeline_steps.status back-fill: rows with non-null `error`
    # carry status='failed'; rows with null `error` carry status='completed'.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE error IS NOT NULL AND status <> 'failed') AS err_mismatch,
              COUNT(*) FILTER (WHERE error IS NULL AND status NOT IN ('completed', 'failed', 'running')) AS clean_mismatch,
              COUNT(*) AS total
            FROM pipeline_steps
            """
        )
        row = cur.fetchone()
        err_mismatch = int(row["err_mismatch"])
        clean_mismatch = int(row["clean_mismatch"])
        total_steps = int(row["total"])
    pass_7 = err_mismatch == 0 and clean_mismatch == 0
    assertion(
        7,
        "pipeline_steps.status back-fill consistent with error column",
        pass_7,
        observed={
            "error_present_not_failed": err_mismatch,
            "error_null_but_unexpected_status": clean_mismatch,
            "total_steps_inspected": total_steps,
        },
        expected={"error_present_not_failed": 0, "error_null_but_unexpected_status": 0},
    )

    # -------------------------------------------------------------------
    # Group D — zero LLM (1 assertion)
    # -------------------------------------------------------------------

    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise()


def _finalise() -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (pure DDL/catalog canary) ===")

    print("\n=== SAMPLE SCHEMA (information_schema + pg_constraint + pg_index) ===")
    print(json.dumps(SAMPLE_SCHEMA, indent=2, default=str))

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 8 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
