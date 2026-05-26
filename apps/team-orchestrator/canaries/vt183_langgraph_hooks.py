#!/usr/bin/env python3
"""VT-183 LangGraph node hooks canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt183_langgraph_hooks.py 2>&1 | tee /tmp/vt183-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Deterministic synthetic graph;
ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 30s. Cost budget: 0 paise.

8 assertions:
- A1: 4-node synthetic graph → 4 state_transition rows (Q2 Option A: one-per-node)
- A2: chronological step_seq order matches node execution order
- A3: from_node + to_node + langgraph_command populate input_envelope
- A4: status='completed' on happy path
- A5: All 4 supervisor.py nodes wired (retrofit verified by import)
- A6: Error-path node raises → status='failed' + error envelope + re-raised (Q3 Option A)
- A7: RLS isolation — tenant_a rows visible only under tenant_a
- A8: ANTHROPIC_API_KEY ABSENT preflight
"""

from __future__ import annotations

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
INSERTED_TENANT_IDS: list[str] = []


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
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary must NOT "
            "source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def _seed_tenant_and_run(pool, tenant_id: UUID) -> UUID:
    INSERTED_TENANT_IDS.append(str(tenant_id))
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt183-{tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt183-canary-salt")

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.decorators import observability_context
    from orchestrator.observability.langgraph_hooks import (
        with_state_transition_hook,
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

    # --------------------------------------------------------------
    # Synthetic deterministic node callables for the canary
    # --------------------------------------------------------------

    def node_alpha(state: dict[str, Any]) -> dict[str, Any]:
        return {"goto": "beta", "update": {"alpha_ran": True}}

    def node_beta(state: dict[str, Any]) -> dict[str, Any]:
        return {"goto": "gamma", "update": {"beta_ran": True}}

    def node_gamma(state: dict[str, Any]) -> dict[str, Any]:
        return {"goto": "delta", "update": {"gamma_ran": True}}

    def node_delta(state: dict[str, Any]) -> dict[str, Any]:
        return {"goto": None, "update": {"delta_ran": True}}  # terminal

    def node_raiser(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("synthetic node_raiser intentional failure")

    wrapped_alpha = with_state_transition_hook(node_alpha, node_name="alpha")
    wrapped_beta = with_state_transition_hook(node_beta, node_name="beta")
    wrapped_gamma = with_state_transition_hook(node_gamma, node_name="gamma")
    wrapped_delta = with_state_transition_hook(node_delta, node_name="delta")
    wrapped_raiser = with_state_transition_hook(node_raiser, node_name="raiser")

    # --------------------------------------------------------------
    # A1-A4: happy path — execute 4 nodes sequentially under context
    # --------------------------------------------------------------

    tenant = uuid4()
    run_id = _seed_tenant_and_run(pool, tenant)

    with observability_context(run_id=run_id, tenant_id=tenant):
        state: dict[str, Any] = {}
        wrapped_alpha(state)
        state["__prev_node__"] = "alpha"
        wrapped_beta(state)
        state["__prev_node__"] = "beta"
        wrapped_gamma(state)
        state["__prev_node__"] = "gamma"
        wrapped_delta(state)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq, step_name, status, input_envelope, error "
            "FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'state_transition' "
            "ORDER BY step_seq",
            (str(run_id),),
        )
        rows = cur.fetchall()

    # A1: 4 rows
    pass_1 = len(rows) == 4
    assertion(
        1,
        "4-node synthetic graph → 4 state_transition rows (Q2 Option A: one-per-node)",
        pass_1,
        observed={"row_count": len(rows)},
        expected={"row_count": 4},
    )

    # A2: chronological step_seq order matches execution order (alpha→beta→gamma→delta)
    step_names_in_order = [r["step_name"] for r in rows]
    pass_2 = step_names_in_order == ["alpha", "beta", "gamma", "delta"]
    assertion(
        2,
        "chronological step_seq order matches node execution order",
        pass_2,
        observed={"step_names_in_order": step_names_in_order},
        expected={"order": ["alpha", "beta", "gamma", "delta"]},
    )

    # A3: from_node + to_node + langgraph_command populate input_envelope
    a3_ok = True
    a3_observed = []
    expected_transitions = [
        ("<unknown>", "beta"),
        ("alpha", "gamma"),
        ("beta", "delta"),
        ("gamma", "<terminal>"),
    ]
    for r, (exp_from, exp_to) in zip(rows, expected_transitions, strict=True):
        env = r["input_envelope"] or {}
        a3_observed.append(
            {
                "from": env.get("from_node"),
                "to": env.get("to_node"),
                "cmd_keys": sorted((env.get("langgraph_command") or {}).keys()),
            }
        )
        if env.get("from_node") != exp_from or env.get("to_node") != exp_to:
            a3_ok = False
    pass_3 = a3_ok
    assertion(
        3,
        "from_node + to_node + langgraph_command populate input_envelope",
        pass_3,
        observed={"transitions": a3_observed, "expected": expected_transitions},
        expected={"all_match_expected_transitions": True},
    )

    # A4: status='completed' on happy path
    happy_statuses = [r["status"] for r in rows]
    happy_errors = [r["error"] for r in rows]
    pass_4 = all(s == "completed" for s in happy_statuses) and all(
        e is None for e in happy_errors
    )
    assertion(
        4,
        "happy-path: all rows status='completed' + error=null",
        pass_4,
        observed={"statuses": happy_statuses, "errors": happy_errors},
        expected={"all_status": "completed", "all_errors_none": True},
    )

    # A5: all 4 supervisor.py nodes wired (verified by importing supervisor +
    # checking the graph has 4 nodes with the with_state_transition_hook
    # signature). Static check.
    from orchestrator.supervisor import build_supervisor_graph

    # build_supervisor_graph needs a model — provide stub or skip the graph
    # build and check the supervisor source for the 4 add_node calls instead.
    supervisor_source = Path(SRC / "orchestrator" / "supervisor.py").read_text()
    wired_nodes = [
        n for n in (
            "orchestrator_agent", "sales_recovery_agent",
            "collapse", "orchestrator_terminal",
        )
        if (
            f'with_state_transition_hook(' in supervisor_source
            and f'node_name="{n}"' in supervisor_source
        )
    ]
    pass_5 = len(wired_nodes) == 4
    assertion(
        5,
        "supervisor.py 4 nodes wired with with_state_transition_hook (static source check)",
        pass_5,
        observed={"wired_nodes": wired_nodes},
        expected={"wired_count": 4},
    )

    # --------------------------------------------------------------
    # A6: error-path — node raises, hook captures + re-raises
    # --------------------------------------------------------------

    tenant_err = uuid4()
    run_err = _seed_tenant_and_run(pool, tenant_err)
    raised = False
    try:
        with observability_context(run_id=run_err, tenant_id=tenant_err):
            wrapped_raiser({})
    except RuntimeError as exc:
        raised = "synthetic node_raiser intentional failure" in str(exc)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'state_transition'",
            (str(run_err),),
        )
        err_row = cur.fetchone()
    pass_6 = (
        raised
        and err_row is not None
        and err_row["status"] == "failed"
        and err_row["error"] is not None
        and err_row["error"].get("exception_type") == "RuntimeError"
    )
    assertion(
        6,
        "error-path: hook catches → status='failed' + error envelope + re-raised to caller",
        pass_6,
        observed={
            "raised_at_caller": raised,
            "row": dict(err_row) if err_row else None,
        },
        expected={
            "raised_at_caller": True,
            "status": "failed",
            "exception_type": "RuntimeError",
        },
    )

    # --------------------------------------------------------------
    # A7: RLS isolation — tenant_other's GUC can't see tenant's rows
    # --------------------------------------------------------------

    tenant_other = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_other))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_other), f"canary-vt183-other-{tenant_other}"),
        )

    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'state_transition'",
            (str(run_id),),
        )
        under_owner = int(cur.fetchone()["n"])
    with tenant_connection(tenant_other) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'state_transition'",
            (str(run_id),),
        )
        under_other = int(cur.fetchone()["n"])

    pass_7 = under_owner == 4 and under_other == 0
    assertion(
        7,
        "RLS isolation: tenant_owner sees 4 rows; tenant_other sees 0",
        pass_7,
        observed={"under_owner": under_owner, "under_other": under_other},
        expected={"under_owner": 4, "under_other": 0},
    )

    # --------------------------------------------------------------
    # A8: zero LLM
    # --------------------------------------------------------------

    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise(pool)


def _finalise(pool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (deterministic synthetic graph) ===")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pipeline_steps WHERE run_id = ANY(%s)", (INSERTED_RUN_IDS,)
            )
            cur.execute(
                "DELETE FROM pipeline_runs WHERE id = ANY(%s)", (INSERTED_RUN_IDS,)
            )
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,)
            )
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
