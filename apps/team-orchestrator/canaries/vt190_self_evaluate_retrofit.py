#!/usr/bin/env python3
"""VT-190 self_evaluate @tool_step retrofit canary (Rule #15, DR-15).

Subshell-source ONLY ``.viabe/secrets/supabase-dev.env``:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt190_self_evaluate_retrofit.py 2>&1 \
        | tee /tmp/vt190-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** The Anthropic client is mocked (deterministic
verdict substrate); ANTHROPIC_API_KEY ABSENT at PREFLIGHT (defense-in-depth
per DR-15). The canary verifies the OBSERVABILITY plumbing of the retrofit
(the pipeline_steps row), not Opus judgment — that lives behind the
``VIABE_RUN_SELF_EVALUATE_CANARY`` opus canary in the unit-test file.

Wall-clock budget <= 30s. Cost budget: 0 paise.

Assertions matching VT-190 §Rule #15 Canary:

- A1: A synthetic ``self_evaluate`` invocation under an ObservabilityContext
  produces exactly ONE pipeline_steps row.
- A2: That row's step_kind='mcp_tool_call' + step_name='self_evaluate'.
  (The tool-level row is the generic mcp_tool_call kind, uniform with
  compose_owner_output_tool. The per-gate-attempt 'self_evaluate_gate' row
  is written separately by sales_recovery._emit_self_evaluate_gate, which is
  retained as a compatibility shim — different granularity.)
- A3: The output_envelope carries the verdict (outcome) + reasons (feedback)
  fields populated from the tool's SelfEvaluateOutput.
- A4: The input_envelope carries tool_args WITHOUT the agent's reasoning
  (Pillar 7 independence holds through the decorator path).
- A5: ANTHROPIC ABSENT preflight (defense-in-depth).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []


def assertion(
    num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {
        "name": name,
        "status": status,
        "observed": observed,
        "expected": expected,
    }
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
            "source anthropic.env (the Opus call is mocked; defense-in-depth "
            "per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def _fake_anthropic(json_text: str) -> MagicMock:
    """A MagicMock Anthropic client whose messages.create returns a single
    text block carrying ``json_text`` — mirrors the unit-test fake."""
    from types import SimpleNamespace

    fake = MagicMock()
    fake.messages.create.return_value = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=200, output_tokens=80),
        content=[SimpleNamespace(type="text", text=json_text)],
        stop_reason="end_turn",
    )
    return fake


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt190-canary-salt")
    # → Haiku slot in models.yaml; never reached (client is mocked) but keeps
    # _resolve_self_evaluate_model deterministic.
    os.environ.setdefault("VIABE_ENV", "test")

    import json

    from orchestrator import graph as graph_mod
    from orchestrator.agent.tools.self_evaluate import (
        SelfEvaluateInput,
        SelfEvaluateTool,
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

    # Inject a deterministic REVISE verdict so the output envelope carries
    # both a verdict (outcome) AND reasons (feedback) — the canary asserts
    # the populated-fields requirement.
    verdict_payload = {
        "outcome": "revise",
        "feedback": {
            "schema": ["target_cohort.cohort_size=200 but customer_ids has 1 entry."],
            "pillar": None,
            "consistency": None,
            "legal": None,
        },
    }
    fake = _fake_anthropic(json.dumps(verdict_payload))
    SelfEvaluateTool._make_client = classmethod(lambda cls: fake)  # type: ignore[assignment]

    obs_run_id = uuid4()
    obs_tenant_id = uuid4()
    INSERTED_RUN_IDS.append(str(obs_run_id))
    INSERTED_TENANT_IDS.append(str(obs_tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(obs_tenant_id), f"canary-vt190-{obs_tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running') ON CONFLICT (id) DO NOTHING",
            (str(obs_run_id), str(obs_tenant_id)),
        )

    draft = {
        "version": "1.0",
        "status": "proposed",
        "target_cohort": {
            "customer_ids": [str(uuid4())],
            "cohort_label": "60-90 day dormants",
            "cohort_size": 200,
            "selection_reason": "canary [E1].",
        },
    }

    with observability_context(run_id=obs_run_id, tenant_id=obs_tenant_id):
        out = SelfEvaluateTool().execute(
            None,  # ctx unused by the wrapped impl (resolved internally)
            SelfEvaluateInput(
                draft_campaign_plan=draft,
                context_summary={},
                attempt_number=1,
            ),
        )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name, input_envelope, output_envelope, status "
            "FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (str(obs_run_id),),
        )
        rows = cur.fetchall()

    # ----------------------------------------------------------------
    # A1: exactly one pipeline_steps row
    # ----------------------------------------------------------------
    pass_1 = len(rows) == 1
    assertion(
        1,
        "synthetic self_evaluate invocation produces exactly ONE pipeline_steps row",
        pass_1,
        observed={"row_count": len(rows)},
        expected={"row_count": 1},
    )

    row = rows[0] if rows else {}

    # ----------------------------------------------------------------
    # A2: step_kind='mcp_tool_call' + step_name='self_evaluate'
    # ----------------------------------------------------------------
    pass_2 = (
        row.get("step_kind") == "mcp_tool_call"
        and row.get("step_name") == "self_evaluate"
    )
    assertion(
        2,
        "row step_kind='mcp_tool_call' + step_name='self_evaluate' (uniform @tool_step path)",
        pass_2,
        observed={
            "step_kind": row.get("step_kind"),
            "step_name": row.get("step_name"),
        },
        expected={"step_kind": "mcp_tool_call", "step_name": "self_evaluate"},
    )

    # ----------------------------------------------------------------
    # A3: output_envelope carries verdict (outcome) + reasons (feedback)
    # ----------------------------------------------------------------
    out_env = row.get("output_envelope") or {}
    tool_result = out_env.get("tool_result") or {}
    pass_3 = (
        tool_result.get("outcome") == "revise"
        and isinstance(tool_result.get("feedback"), dict)
        and tool_result["feedback"].get("schema")  # populated reasons list
    )
    assertion(
        3,
        "output_envelope.tool_result carries verdict (outcome) + reasons (feedback) populated",
        pass_3,
        observed={
            "outcome": tool_result.get("outcome"),
            "feedback_schema": (tool_result.get("feedback") or {}).get("schema"),
        },
        expected={"outcome": "revise", "feedback_schema": "<non-empty list>"},
    )

    # ----------------------------------------------------------------
    # A4: input_envelope carries tool_args WITHOUT agent reasoning (Pillar 7)
    # ----------------------------------------------------------------
    in_env = row.get("input_envelope") or {}
    tool_args = in_env.get("tool_args") or {}
    pass_4 = (
        in_env.get("tool_name") == "self_evaluate"
        and tool_args.get("attempt_number") == 1
        and "reasoning_chain" not in tool_args
        and "draft_campaign_plan" in tool_args
    )
    assertion(
        4,
        "input_envelope.tool_args carries draft + attempt_number, NO reasoning_chain (Pillar 7)",
        pass_4,
        observed={
            "tool_name": in_env.get("tool_name"),
            "tool_args_keys": sorted(tool_args.keys()),
        },
        expected={
            "tool_name": "self_evaluate",
            "tool_args_keys_excludes": "reasoning_chain",
        },
    )

    # Sanity: the tool still returned its verdict (decorator is pass-through).
    print(f"    tool returned outcome={out.outcome!r}")

    # ----------------------------------------------------------------
    # A5: ANTHROPIC ABSENT (defense-in-depth)
    # ----------------------------------------------------------------
    pass_5 = not os.environ.get("ANTHROPIC_API_KEY")
    assertion(
        5,
        "ANTHROPIC_API_KEY absent throughout (Opus mocked; defense-in-depth DR-15)",
        pass_5,
        observed={"ANTHROPIC_API_KEY": "<absent>" if pass_5 else "<PRESENT — FAIL>"},
        expected={"ANTHROPIC_API_KEY": "<absent>"},
    )

    return _finalise(pool)


def _finalise(pool: Any) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (mocked client; no real LLM call) ===")

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
