#!/usr/bin/env python3
"""VT-181 ``@observability.tool_step`` decorator canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt181_tool_decorator.py 2>&1 | tee /tmp/vt181-canary-evidence.log | tail -180
    )

**NO anthropic.env sourced.** Deterministic synthetic tool;
ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 30s. Cost budget: 0 paise.

8 assertions across 5 groups:
- A1-A2: basic invocation + envelope captured in pipeline_steps row
- B3-B5: envelope-in validation soft-fail + envelope-out validation soft-fail
  + exception path emits status=failed
- C6: compose_owner_output retrofit fires via observability_context
- D7: TOOL_STEP_REGISTRY drift detection (synthetic decorated tool with
  unregistered step_kind → EnvelopeRegistryDrift)
- E8: zero-LLM invariant
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
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary's loader "
            "must NOT source anthropic.env (defense-in-depth per DR-15).",
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
            (str(tenant_id), f"canary-vt181-{tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt181-canary-salt")

    from pydantic import BaseModel, ConfigDict

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

    from orchestrator.observability.decorators import (
        TOOL_STEP_REGISTRY,
        observability_context,
        tool_step,
        validate_tool_step_registry,
    )
    from orchestrator.observability.envelopes import EnvelopeRegistryDrift

    # ----------------------------------------------------------------
    # Define synthetic decorated tools for the canary
    # ----------------------------------------------------------------

    class _SyntheticInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int
        y: int

    class _SyntheticOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        result: int

    @tool_step(
        step_kind="mcp_tool_call",
        envelope_in=_SyntheticInput,
        envelope_out=_SyntheticOutput,
        step_name="synthetic_adder",
    )
    def synthetic_adder(*, x: int, y: int) -> _SyntheticOutput:
        return _SyntheticOutput(result=x + y)

    @tool_step(
        step_kind="mcp_tool_call",
        envelope_in=_SyntheticInput,
        envelope_out=_SyntheticOutput,
        step_name="synthetic_raiser",
    )
    def synthetic_raiser(*, x: int, y: int) -> _SyntheticOutput:
        raise RuntimeError("synthetic_raiser intentional failure")

    # ----------------------------------------------------------------
    # Group A — basic invocation (2 assertions)
    # ----------------------------------------------------------------

    tenant_a = uuid4()
    run_a = _seed_tenant_and_run(pool, tenant_a)

    with observability_context(run_id=run_a, tenant_id=tenant_a):
        out = synthetic_adder(x=3, y=4)
    assert out.result == 7, "synthetic_adder returned wrong value"

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name, status, input_envelope, output_envelope, error "
            "FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (str(run_a),),
        )
        rows = cur.fetchall()

    pass_1 = (
        len(rows) == 1
        and rows[0]["step_kind"] == "mcp_tool_call"
        and rows[0]["step_name"] == "synthetic_adder"
        and rows[0]["status"] == "completed"
    )
    assertion(
        1,
        "decorated tool invocation: pipeline_steps row with step_kind + step_name + status='completed'",
        pass_1,
        observed={
            "row_count": len(rows),
            "row": dict(rows[0]) if rows else None,
        },
        expected={
            "step_kind": "mcp_tool_call",
            "step_name": "synthetic_adder",
            "status": "completed",
        },
    )

    # The decorator wraps the tool's args/result in the VT-179
    # mcp_tool_call envelope shape: input = {tool_name, tool_args},
    # output = {tool_result, cost_paise, duration_ms}.
    input_env = rows[0]["input_envelope"]
    output_env = rows[0]["output_envelope"]
    pass_2 = (
        input_env.get("tool_name") == "synthetic_adder"
        and input_env.get("tool_args") == {"x": 3, "y": 4}
        and output_env.get("tool_result") == {"result": 7}
        and output_env.get("cost_paise") == 0
        and isinstance(output_env.get("duration_ms"), int)
        and rows[0]["error"] is None
    )
    assertion(
        2,
        "decorated tool: VT-179 mcp_tool_call envelope shape (tool_name+tool_args / tool_result+cost+duration) + error=null",
        pass_2,
        observed={
            "input_envelope": input_env,
            "output_envelope": output_env,
            "error": rows[0]["error"],
        },
        expected={
            "input_envelope_keys": ["tool_args", "tool_name"],
            "input_envelope.tool_name": "synthetic_adder",
            "input_envelope.tool_args": {"x": 3, "y": 4},
            "output_envelope.tool_result": {"result": 7},
        },
    )

    # ----------------------------------------------------------------
    # Group B — validation soft-fail + exception path (3 assertions)
    # ----------------------------------------------------------------

    # B3: envelope_in mismatch (string where int expected) → soft-fail flag.
    class _Wide(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int | str
        y: int | str

    @tool_step(
        step_kind="mcp_tool_call",
        envelope_in=_SyntheticInput,
        envelope_out=_SyntheticOutput,
        step_name="synthetic_bad_input",
    )
    def synthetic_bad_input(*, x: Any, y: Any) -> _SyntheticOutput:
        # Cast through int so function works regardless of validation
        return _SyntheticOutput(result=int(str(x))[0] if False else 0)

    with observability_context(run_id=run_a, tenant_id=tenant_a):
        synthetic_bad_input(x="not-an-int", y="not-an-int-either")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error FROM pipeline_steps "
            "WHERE run_id = %s AND step_name = 'synthetic_bad_input'",
            (str(run_a),),
        )
        b3_row = cur.fetchone()
    pass_3 = (
        b3_row is not None
        and b3_row["status"] == "failed"
        and b3_row["error"] is not None
        and b3_row["error"].get("payload_validation_failed") is True
    )
    assertion(
        3,
        "envelope_in mismatch: row written + payload_validation_failed=true + status='failed'",
        pass_3,
        observed={"row": dict(b3_row) if b3_row else None},
        expected={"status": "failed", "payload_validation_failed": True},
    )

    # B4: envelope_out mismatch — tool returns wrong shape.
    @tool_step(
        step_kind="mcp_tool_call",
        envelope_in=_SyntheticInput,
        envelope_out=_SyntheticOutput,
        step_name="synthetic_bad_output",
    )
    def synthetic_bad_output(*, x: int, y: int) -> dict[str, Any]:
        # Return shape doesn't match _SyntheticOutput.
        return {"oops_no_result_field": x + y}

    with observability_context(run_id=run_a, tenant_id=tenant_a):
        synthetic_bad_output(x=1, y=2)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error FROM pipeline_steps "
            "WHERE run_id = %s AND step_name = 'synthetic_bad_output'",
            (str(run_a),),
        )
        b4_row = cur.fetchone()
    pass_4 = (
        b4_row is not None
        and b4_row["status"] == "failed"
        and b4_row["error"] is not None
        and b4_row["error"].get("output_validation_failed") is True
    )
    assertion(
        4,
        "envelope_out mismatch: row written + output_validation_failed=true",
        pass_4,
        observed={"row": dict(b4_row) if b4_row else None},
        expected={"status": "failed", "output_validation_failed": True},
    )

    # B5: wrapped function raises → row written status=failed + exception captured + re-raised.
    raised = False
    try:
        with observability_context(run_id=run_a, tenant_id=tenant_a):
            synthetic_raiser(x=1, y=2)
    except RuntimeError as exc:
        raised = "synthetic_raiser intentional failure" in str(exc)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error FROM pipeline_steps "
            "WHERE run_id = %s AND step_name = 'synthetic_raiser'",
            (str(run_a),),
        )
        b5_row = cur.fetchone()
    pass_5 = (
        raised
        and b5_row is not None
        and b5_row["status"] == "failed"
        and b5_row["error"] is not None
        and b5_row["error"].get("exception_type") == "RuntimeError"
    )
    assertion(
        5,
        "wrapped raise: row written + status='failed' + exception captured + re-raised to caller",
        pass_5,
        observed={
            "raised_at_caller": raised,
            "row": dict(b5_row) if b5_row else None,
        },
        expected={
            "raised_at_caller": True,
            "status": "failed",
            "exception_type": "RuntimeError",
        },
    )

    # ----------------------------------------------------------------
    # Group C — compose_owner_output retrofit (1 assertion)
    # ----------------------------------------------------------------

    # The retrofitted compose_owner_output_tool is wrapped with both
    # langchain @tool (outermost) and @tool_step. The langchain Tool
    # object exposes the wrapper as a callable through .invoke(args).
    from orchestrator.agent.tools.compose_output import (
        compose_owner_output_tool,
    )

    tenant_c = uuid4()
    run_c = _seed_tenant_and_run(pool, tenant_c)

    with observability_context(run_id=run_c, tenant_id=tenant_c):
        result = compose_owner_output_tool.invoke(
            {
                "intent_or_trigger": "welcome",
                "tenant_id": str(tenant_c),
                "phase": "onboarding",
                "last_owner_message_at_iso": None,
                "escalation_pending": False,
                "specialist_result_json": None,
            }
        )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name, status FROM pipeline_steps "
            "WHERE run_id = %s ORDER BY step_seq",
            (str(run_c),),
        )
        c_rows = cur.fetchall()
    pass_6 = (
        len(c_rows) == 1
        and c_rows[0]["step_kind"] == "mcp_tool_call"
        and c_rows[0]["step_name"] == "compose_owner_output"
        and c_rows[0]["status"] == "completed"
        and isinstance(result, dict)
        and "message_body" in result
    )
    assertion(
        6,
        "compose_owner_output retrofit: row written via observability_context + langchain @tool integration intact",
        pass_6,
        observed={
            "row_count": len(c_rows),
            "row": dict(c_rows[0]) if c_rows else None,
            "result_keys": sorted(result.keys()) if isinstance(result, dict) else None,
        },
        expected={
            "step_kind": "mcp_tool_call",
            "step_name": "compose_owner_output",
            "status": "completed",
        },
    )

    # ----------------------------------------------------------------
    # Group D — drift detection (1 assertion)
    # ----------------------------------------------------------------

    # Register a tool with unregistered step_kind; expect EnvelopeRegistryDrift.
    # Snapshot then restore TOOL_STEP_REGISTRY to keep the rest of the canary clean.
    snapshot = dict(TOOL_STEP_REGISTRY)
    drift_raised = False
    try:
        @tool_step(
            step_kind="definitely_not_a_registered_step_kind",
            envelope_in=_SyntheticInput,
            envelope_out=_SyntheticOutput,
            step_name="drift_synthetic",
        )
        def _drifted(*, x: int, y: int) -> _SyntheticOutput:
            return _SyntheticOutput(result=x + y)

        validate_tool_step_registry()
    except EnvelopeRegistryDrift:
        drift_raised = True
    finally:
        TOOL_STEP_REGISTRY.clear()
        TOOL_STEP_REGISTRY.update(snapshot)

    pass_7 = drift_raised
    assertion(
        7,
        "drift detection: @tool_step with unregistered step_kind → EnvelopeRegistryDrift at validate_tool_step_registry()",
        pass_7,
        observed={"drift_raised": drift_raised},
        expected={"drift_raised": True},
    )

    # ----------------------------------------------------------------
    # Group E — zero LLM (1 assertion)
    # ----------------------------------------------------------------

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

    print("\n=== Anthropic cost: 0 paise (deterministic synthetic tools) ===")

    # Cleanup. Service-role bypasses RLS.
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
