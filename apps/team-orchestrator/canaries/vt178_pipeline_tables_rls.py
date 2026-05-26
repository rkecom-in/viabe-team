#!/usr/bin/env python3
"""VT-178 pipeline tables + RLS hardening canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt178_pipeline_tables_rls.py 2>&1 | tee /tmp/vt178-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Defense-in-depth Pillar 1: this canary is
pure DDL inspection + RLS round-trip; ANTHROPIC_API_KEY ABSENT at PREFLIGHT.
Wall-clock budget ≤ 30s. Cost budget: 0 paise.

8 assertions across 4 groups (A column-level audit / B RLS isolation /
C index presence / D zero-LLM). Column audit asserts CANONICAL §2.1
columns (post-VT-187 / migration 025 schema normalization — column
renames applied, additive canonical columns present, customer_id
present without FK per CL-417 Cond 1).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_STEP_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []
INSERTED_TOKEN_IDS: list[str] = []
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


def _seed_tenant(pool, tenant_id: UUID):
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt178-{tenant_id}"),
        )


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt178-canary-salt")

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

    # -------------------------------------------------------------------
    # Group A — column-level audit (3 assertions)
    # asserting ACTUAL on-main schema per Q1+Q3 review verdict
    # -------------------------------------------------------------------

    def _cols(table: str) -> set[str]:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s "
                "ORDER BY ordinal_position",
                (table,),
            )
            return {row["column_name"] for row in cur.fetchall()}

    # Assertion 1 — pipeline_runs CANONICAL §2.1 columns (post-VT-187).
    expected_pr = {
        "id", "tenant_id", "run_type", "status", "started_at", "ended_at",
        "trigger_payload", "terminal_state_metadata",
        # VT-187 canonical additions + rename:
        "trigger_kind", "trigger_source_ref", "final_outcome",
        "step_count", "error_summary", "total_cost_paise",
    }
    actual_pr = _cols("pipeline_runs")
    SAMPLE_SCHEMA["pipeline_runs"] = sorted(actual_pr)
    pass_1 = expected_pr <= actual_pr and "cost_paise" not in actual_pr
    assertion(
        1,
        "pipeline_runs CANONICAL §2.1 columns present; cost_paise renamed",
        pass_1,
        observed={
            "actual_columns": sorted(actual_pr),
            "missing": sorted(expected_pr - actual_pr),
            "legacy_cost_paise_present": "cost_paise" in actual_pr,
        },
        expected={"superset_of": sorted(expected_pr), "legacy_cost_paise_present": False},
    )

    # Assertion 2 — pipeline_steps CANONICAL §2.1 columns (post-VT-187).
    expected_ps = {
        "id", "run_id", "tenant_id", "step_seq", "step_kind",
        "input_envelope", "output_envelope", "decision_rationale",
        "started_at", "ended_at", "cost_paise", "duration_ms", "error",
        # VT-187 canonical additions:
        "step_name", "parent_step_id", "tool_calls", "status",
        "model_used", "tokens_input", "tokens_output",
    }
    actual_ps = _cols("pipeline_steps")
    SAMPLE_SCHEMA["pipeline_steps"] = sorted(actual_ps)
    legacy_present = bool({"step_index", "rationale", "error_envelope"} & actual_ps)
    pass_2 = expected_ps <= actual_ps and not legacy_present
    assertion(
        2,
        "pipeline_steps CANONICAL §2.1 columns present; legacy names retired",
        pass_2,
        observed={
            "actual_columns": sorted(actual_ps),
            "missing": sorted(expected_ps - actual_ps),
            "legacy_present": sorted({"step_index", "rationale", "error_envelope"} & actual_ps),
        },
        expected={"superset_of": sorted(expected_ps), "legacy_present": []},
    )

    # Assertion 3 — phone_token_resolutions CANONICAL §2.1 columns (post-VT-187).
    expected_pt = {
        "phone_token", "tenant_id", "phone_number_encrypted",
        "resolved_count", "last_accessed_at", "created_at",
        # VT-187 canonical addition (NO FK per CL-417 Cond 1):
        "customer_id",
    }
    actual_pt = _cols("phone_token_resolutions")
    SAMPLE_SCHEMA["phone_token_resolutions"] = sorted(actual_pt)
    legacy_pt_present = bool({"token", "last_resolved_at"} & actual_pt)
    pass_3 = expected_pt <= actual_pt and not legacy_pt_present
    assertion(
        3,
        "phone_token_resolutions CANONICAL §2.1 columns present; legacy names retired",
        pass_3,
        observed={
            "actual_columns": sorted(actual_pt),
            "missing": sorted(expected_pt - actual_pt),
            "legacy_present": sorted({"token", "last_resolved_at"} & actual_pt),
        },
        expected={"superset_of": sorted(expected_pt), "legacy_present": []},
    )

    # -------------------------------------------------------------------
    # Group B — RLS isolation (3 assertions)
    # -------------------------------------------------------------------

    tenant_a = uuid4()
    tenant_b = uuid4()
    _seed_tenant(pool, tenant_a)
    _seed_tenant(pool, tenant_b)

    # Seed via service-role insert (bypasses RLS for setup).
    run_a = uuid4()
    INSERTED_RUN_IDS.append(str(run_a))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) VALUES (%s, %s, 'completed')",
            (str(run_a), str(tenant_a)),
        )
        cur.execute(
            "INSERT INTO pipeline_steps (id, run_id, tenant_id, step_seq, step_kind, started_at, status) "
            "VALUES (gen_random_uuid(), %s, %s, 1, 'canary', now(), 'completed') RETURNING id",
            (str(run_a), str(tenant_a)),
        )
        INSERTED_STEP_IDS.append(str(cur.fetchone()["id"]))
        token_a = f"cust_tok_{uuid4().hex[:24]}"
        INSERTED_TOKEN_IDS.append(token_a)
        cur.execute(
            "INSERT INTO phone_token_resolutions (phone_token, tenant_id, phone_number_encrypted) "
            "VALUES (%s, %s, %s)",
            (token_a, str(tenant_a), "encrypted_blob"),
        )

    # Assertion 4 — pipeline_runs RLS isolation via tenant_connection().
    from orchestrator.db import tenant_connection
    with tenant_connection(tenant_a) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_runs WHERE id = %s", (str(run_a),))
        count_a = int(cur.fetchone()["c"])
    with tenant_connection(tenant_b) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_runs WHERE id = %s", (str(run_a),))
        count_b = int(cur.fetchone()["c"])
    pass_4 = count_a == 1 and count_b == 0
    assertion(
        4,
        "pipeline_runs RLS isolation: tenant_A sees its row; tenant_B does not",
        pass_4,
        observed={"tenant_a_count": count_a, "tenant_b_count": count_b},
        expected={"tenant_a_count": 1, "tenant_b_count": 0},
    )

    # Assertion 5 — pipeline_steps RLS isolation via tenant_connection().
    with tenant_connection(tenant_a) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_steps WHERE run_id = %s", (str(run_a),))
        step_count_a = int(cur.fetchone()["c"])
    with tenant_connection(tenant_b) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM pipeline_steps WHERE run_id = %s", (str(run_a),))
        step_count_b = int(cur.fetchone()["c"])
    pass_5 = step_count_a == 1 and step_count_b == 0
    assertion(
        5,
        "pipeline_steps RLS isolation: tenant_A sees its step; tenant_B does not",
        pass_5,
        observed={"tenant_a_count": step_count_a, "tenant_b_count": step_count_b},
        expected={"tenant_a_count": 1, "tenant_b_count": 0},
    )

    # Assertion 6 — phone_token_resolutions stricter access.
    # STEP-0 finding (revised understanding): 015_app_role.sql does NOT
    # grant SELECT/INSERT/UPDATE/DELETE on phone_token_resolutions to
    # app_role. The stricter access pattern is BY-GRANT-EXCLUSION rather
    # than by RLS policy variation. app_role under tenant_connection()
    # raises `psycopg.errors.InsufficientPrivilege` on any access. The
    # service-role path retains full access. This is the structural
    # enforcement of brief §"phone_token_resolutions stricter RLS";
    # operator-role-policy refinement deferred to VT-188.
    import psycopg

    privilege_blocked_a = False
    privilege_blocked_b = False
    try:
        with tenant_connection(tenant_a) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM phone_token_resolutions WHERE phone_token = %s",
                (token_a,),
            )
            _ = cur.fetchone()
    except psycopg.errors.InsufficientPrivilege:
        privilege_blocked_a = True
    try:
        with tenant_connection(tenant_b) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM phone_token_resolutions WHERE phone_token = %s",
                (token_a,),
            )
            _ = cur.fetchone()
    except psycopg.errors.InsufficientPrivilege:
        privilege_blocked_b = True

    # Service-role retains read access (verified positively).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM phone_token_resolutions WHERE phone_token = %s",
            (token_a,),
        )
        service_count = int(cur.fetchone()["c"])
    pass_6 = (
        privilege_blocked_a is True
        and privilege_blocked_b is True
        and service_count == 1
    )
    assertion(
        6,
        "phone_token_resolutions stricter access: app_role denied (BY-GRANT-EXCLUSION); service-role reads OK",
        pass_6,
        observed={
            "app_role_tenant_a_blocked": privilege_blocked_a,
            "app_role_tenant_b_blocked": privilege_blocked_b,
            "service_role_count": service_count,
        },
        expected={
            "app_role_tenant_a_blocked": True,
            "app_role_tenant_b_blocked": True,
            "service_role_count": 1,
        },
    )

    # -------------------------------------------------------------------
    # Group C — index presence (1 assertion)
    # -------------------------------------------------------------------

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' "
            "  AND tablename IN ('pipeline_runs','pipeline_steps','phone_token_resolutions') "
            "ORDER BY indexname"
        )
        idx_names = {row["indexname"] for row in cur.fetchall()}
    required = {
        "pipeline_runs_tenant_started_idx",
        "pipeline_steps_run_step_idx",
        "pipeline_steps_tenant_started_idx",
        "phone_token_resolutions_tenant_token_idx",
    }
    pass_7 = required <= idx_names
    SAMPLE_SCHEMA["indexes"] = sorted(idx_names)
    assertion(
        7,
        "Required composite indexes present per VT-178 migration 024",
        pass_7,
        observed={"all_indexes": sorted(idx_names), "missing": sorted(required - idx_names)},
        expected={"required_present": sorted(required)},
    )

    # -------------------------------------------------------------------
    # Group D — zero LLM (1 assertion)
    # -------------------------------------------------------------------

    # Assertion 8 — defense-in-depth runtime check.
    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise(pool)


def _finalise(pool):
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (pure DDL/RLS canary) ===")

    print("\n=== SAMPLE SCHEMA (information_schema.columns + pg_indexes) ===")
    print(json.dumps(SAMPLE_SCHEMA, indent=2, default=str))

    # Cleanup. Service-role bypasses RLS.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM pipeline_steps WHERE id = ANY(%s)", (INSERTED_STEP_IDS,))
            cur.execute("DELETE FROM pipeline_runs WHERE id = ANY(%s)", (INSERTED_RUN_IDS,))
            cur.execute("DELETE FROM phone_token_resolutions WHERE phone_token = ANY(%s)", (INSERTED_TOKEN_IDS,))
            cur.execute("DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,))
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 8 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
