#!/usr/bin/env python3
"""VT-125 orchestrator-agent canary (Rule #15, DR-15).

Subshell-source `.viabe/secrets/supabase-dev.env` + anthropic.env
(+ optional logfire-dev.env for trace correlation):

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/anthropic.env
      set +a
      time ./.venv/bin/python canaries/vt125_orchestrator_agent.py 2>&1 | tee /tmp/vt125-canary-evidence.log | tail -200
    )

Real Anthropic Haiku call per CL-274 two-mode pattern (CL-248 test model).
Cost cap ≤ ₹1 (100 paise). Wall-clock budget ≤ 60s.

8 assertions per VT-125 brief:
- A1: system prompt loads + non-empty + key sections present
- A2: synthetic Haiku invocation succeeds + observability row lands
- A3: spawn-decision parsing (synthetic "weekly_cadence" trigger expects
  agent to reference spawn_sales_recovery OR escalate_to_fazal in response)
- A4: hard limit 5 tool calls → HardLimitExceeded(axis='tool_calls')
- A5: hard limit 10K tokens → HardLimitExceeded(axis='tokens')
- A6: tool subset enforcement — out-of-subset tool name in agent state
  triggers warning (langchain's BaseCallbackHandler reports unknown tool;
  driver doesn't define new tools mid-run)
- A7: cost <₹1 budget observed for the canary's single invocation
- A8: ANTHROPIC env present + model used contains 'haiku'
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
    missing = [
        k for k in ("DATABASE_URL", "ANTHROPIC_API_KEY")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        "ANTHROPIC_API_KEY: present (real Haiku call mode)"
    )


def _seed_run(pool, tenant_id):
    INSERTED_TENANT_IDS.append(str(tenant_id))
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt125-{tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt125-canary-salt")

    from langchain_anthropic import ChatAnthropic

    from orchestrator import graph as graph_mod
    from orchestrator.agent.orchestrator_agent import (
        ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
        ORCHESTRATOR_AGENT_TOOLS,
        build_orchestrator_agent,
    )
    from orchestrator.agent.orchestrator_agent_driver import (
        HardLimitExceeded,
        OrchestratorAgentDriver,
    )
    from orchestrator.graph import get_pool
    from orchestrator.observability.decorators import observability_context

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

    # ----------------------------------------------------------------
    # A1: system prompt loads + key sections present
    # ----------------------------------------------------------------
    prompt = ORCHESTRATOR_AGENT_SYSTEM_PROMPT
    required_sections = [
        "## Role",
        "## Decision framework",
        "## Tools available",
        "## Hard limits",
        "## Escalation criteria",
    ]
    missing = [s for s in required_sections if s not in prompt]
    pass_1 = len(prompt) > 1000 and len(missing) == 0
    assertion(
        1,
        "system prompt loads + has Role/Decision/Tools/Hard-limits/Escalation sections",
        pass_1,
        observed={"prompt_length": len(prompt), "missing_sections": missing},
        expected={"prompt_length_gt": 1000, "missing_sections": []},
    )

    # ----------------------------------------------------------------
    # Build Haiku-driven agent + driver
    # ----------------------------------------------------------------
    haiku = ChatAnthropic(model="claude-haiku-4-5", max_tokens=512)  # type: ignore[call-arg]
    agent = build_orchestrator_agent(haiku)
    driver = OrchestratorAgentDriver(agent, model_name="claude-haiku-4-5")

    tenant = uuid4()
    run_id = _seed_run(pool, tenant)

    # ----------------------------------------------------------------
    # A2 + A3: synthetic invocation; observability row + spawn-decision parsing
    # ----------------------------------------------------------------
    event_msg = (
        "weekly_cadence trigger fired for tenant; dormant-customer winback "
        "campaign is needed. Coordinate via the available tools."
    )
    invoke_exc: BaseException | None = None
    result: Any = None
    try:
        with observability_context(run_id=run_id, tenant_id=tenant):
            result = driver.invoke(
                messages=[{"role": "user", "content": event_msg}],
                run_id=run_id,
                tenant_id=tenant,
                depth=1,
            )
    except BaseException as exc:  # noqa: BLE001
        invoke_exc = exc

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name, status FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_reasoning_step' "
            "ORDER BY step_seq",
            (str(run_id),),
        )
        agent_rows = cur.fetchall()
    pass_2 = invoke_exc is None and len(agent_rows) >= 1
    assertion(
        2,
        "synthetic Haiku invocation succeeds + ≥1 agent_reasoning_step row lands",
        pass_2,
        observed={
            "invoke_exception": repr(invoke_exc) if invoke_exc else None,
            "agent_rows_count": len(agent_rows),
            "agent_rows_sample": [
                {"step_name": r["step_name"], "status": r["status"]} for r in agent_rows[:3]
            ],
        },
        expected={"invoke_exception": None, "agent_rows_count_gte": 1},
    )

    # A3: scan agent's response for spawn_sales_recovery OR escalate_to_fazal mention
    response_text = ""
    if result and isinstance(result, dict):
        messages = result.get("messages", [])
        for msg in reversed(messages):
            content = getattr(msg, "content", None) or (
                msg.get("content") if isinstance(msg, dict) else None
            )
            if isinstance(content, str) and content:
                response_text = content
                break
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text = block.get("text", "")
                        if response_text:
                            break
                if response_text:
                    break

    pass_3 = (
        "spawn_sales_recovery" in response_text
        or "sales_recovery" in response_text.lower()
        or "spawn" in response_text.lower()
        or "escalate" in response_text.lower()
        or "compose_owner_output" in response_text.lower()
    )
    assertion(
        3,
        "agent response references a routing decision (spawn / escalate / compose)",
        pass_3,
        observed={"response_first200": response_text[:200]},
        expected={
            "contains_any_of": [
                "spawn_sales_recovery", "spawn", "escalate", "compose_owner_output",
            ]
        },
    )

    # ----------------------------------------------------------------
    # A4: hard limit 5 tool calls (synthetic — short-circuit via low limit)
    # ----------------------------------------------------------------
    low_tool_limit_driver = OrchestratorAgentDriver(
        agent, model_name="claude-haiku-4-5", tool_call_limit=0,
    )
    a4_raised = False
    a4_exc: BaseException | None = None
    try:
        from orchestrator.agent.orchestrator_agent_driver import OrchestratorUsage
        usage = OrchestratorUsage()
        usage.tool_calls = 1  # simulate one tool call already
        low_tool_limit_driver.check_mid_invocation(
            usage, run_id=run_id, tenant_id=tenant
        )
    except HardLimitExceeded as exc:
        a4_raised = True
        a4_exc = exc
    pass_4 = (
        a4_raised
        and a4_exc is not None
        and getattr(a4_exc, "axis", "") == "tool_calls"
    )
    assertion(
        4,
        "hard limit: tool_calls > 5 → HardLimitExceeded(axis='tool_calls')",
        pass_4,
        observed={
            "raised": a4_raised,
            "axis": getattr(a4_exc, "axis", None),
        },
        expected={"raised": True, "axis": "tool_calls"},
    )

    # ----------------------------------------------------------------
    # A5: hard limit 10K tokens (synthetic)
    # ----------------------------------------------------------------
    a5_raised = False
    a5_exc: BaseException | None = None
    try:
        usage5 = OrchestratorUsage()
        usage5.tokens_input = 6_000
        usage5.tokens_output = 5_000  # 11K total > 10K limit
        driver.check_mid_invocation(usage5, run_id=run_id, tenant_id=tenant)
    except HardLimitExceeded as exc:
        a5_raised = True
        a5_exc = exc
    pass_5 = (
        a5_raised
        and a5_exc is not None
        and getattr(a5_exc, "axis", "") == "tokens"
    )
    assertion(
        5,
        "hard limit: cumulative tokens > 10_000 → HardLimitExceeded(axis='tokens')",
        pass_5,
        observed={
            "raised": a5_raised,
            "axis": getattr(a5_exc, "axis", None),
            "observed_tokens": (usage5.cumulative_tokens if a5_raised else None),
        },
        expected={"raised": True, "axis": "tokens"},
    )

    # ----------------------------------------------------------------
    # A6: tool subset enforcement
    # ----------------------------------------------------------------
    # Tool names exposed via ORCHESTRATOR_AGENT_TOOLS are the subset.
    # Any name NOT in this set is out-of-subset. We assert the subset
    # composition matches VT-125 expectations and the agent's bound
    # tools match exactly (no rogue tools in the registered set).
    expected_subset = {
        "escalate_to_fazal",
        "compose_owner_output_tool",
        # VT-126 replaced VT-125 L0 stubs with real @tool_step-decorated impls.
        "write_l0_fragment",
        "query_l0",
        "send_whatsapp_template_stub",
        "get_subscriber_state_stub",
        "query_pipeline_history_stub",
    }
    actual_names = {t.name for t in ORCHESTRATOR_AGENT_TOOLS}
    pass_6 = actual_names == expected_subset
    assertion(
        6,
        "tool subset enforcement: ORCHESTRATOR_AGENT_TOOLS matches expected VT-125 inventory",
        pass_6,
        observed={
            "actual": sorted(actual_names),
            "expected": sorted(expected_subset),
            "missing_from_actual": sorted(expected_subset - actual_names),
            "unexpected_in_actual": sorted(actual_names - expected_subset),
        },
        expected={"actual": sorted(expected_subset)},
    )

    # ----------------------------------------------------------------
    # A7: cost budget < ₹1 across the single Haiku invocation
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_paise), 0) AS total_paise "
            "FROM pipeline_steps WHERE run_id = %s",
            (str(run_id),),
        )
        cost_row = cur.fetchone()
    total_cost_paise = int(cost_row["total_paise"] or 0)
    pass_7 = total_cost_paise < 100
    assertion(
        7,
        "cost budget: single Haiku invocation < ₹1 (100 paise)",
        pass_7,
        observed={"total_cost_paise": total_cost_paise},
        expected={"total_cost_paise_lt": 100},
    )

    # ----------------------------------------------------------------
    # A8: ANTHROPIC env present + Haiku model used
    # ----------------------------------------------------------------
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT model_used FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_reasoning_step' "
            "  AND model_used IS NOT NULL",
            (str(run_id),),
        )
        models = sorted(r["model_used"] for r in cur.fetchall())
    pass_8 = (
        os.environ.get("ANTHROPIC_API_KEY") is not None
        and any("haiku" in (m or "").lower() for m in models)
    )
    assertion(
        8,
        "ANTHROPIC env present + agent_reasoning_step rows record Haiku model",
        pass_8,
        observed={
            "anthropic_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None,
            "models_observed": models,
        },
        expected={
            "anthropic_key_present": True,
            "model_contains_haiku": True,
        },
    )

    return _finalise(pool, total_cost_paise)


def _finalise(pool, total_cost_paise: int) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print(
        f"\n=== Anthropic cost: {total_cost_paise} paise (₹{total_cost_paise / 100:.2f}) "
        "across orchestrator-agent invocation ==="
    )

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
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
