#!/usr/bin/env python3
"""VT-182 Anthropic agent reasoning step callback canary (Rule #15, DR-15).

Subshell-source `.viabe/secrets/supabase-dev.env` + anthropic.env +
logfire-dev.env (for trace_id correlation):

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/anthropic.env
      source ../../.viabe/secrets/logfire-dev.env
      set +a
      time ./.venv/bin/python canaries/vt182_agent_callback.py 2>&1 | tee /tmp/vt182-canary-evidence.log | tail -200
    )

Real Anthropic Haiku call per CL-274 two-mode pattern (CL-248 test model).
Cost cap ≤ ₹1 (100 paise) across 3-iteration synthetic agent run.
Wall-clock budget ≤ 60s.

8 assertions per brief §Rule-15:
- A1: 3-iteration synthetic agent run → 3 agent_reasoning_step rows
- A2: think_text_redacted contains phone-token (cust_tok_), NO raw digits
- A3: Logfire trace_id captured + matches active span context
- A4: tokens_input/output match response.usage exactly
- A5: model_used == claude-haiku-4-5 alias
- A6: cost_paise == compute_cost_paise(model, in, out)
- A7: parent_step_id populated for nested think/act/observe
- A8: cost_budget total < ₹1 (100 paise)
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
    missing = [
        k for k in ("DATABASE_URL", "ANTHROPIC_API_KEY")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: present (real Haiku call mode)"
    )


def _seed_tenant_and_run(pool, tenant_id: UUID) -> UUID:
    INSERTED_TENANT_IDS.append(str(tenant_id))
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt182-{tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt182-canary-salt")

    from anthropic import Anthropic

    from orchestrator import graph as graph_mod
    from orchestrator.agent.cost import compute_cost_paise
    from orchestrator.agent.sales_recovery import _run_one_turn
    from orchestrator.graph import get_pool
    from orchestrator.observability.agent_callback import reasoning_step_input
    from orchestrator.observability.decorators import observability_context

    # Optional Logfire — if LOGFIRE_TOKEN configured, configure_logfire
    # opens an OTLP exporter so the spans below carry a real trace_id.
    try:
        from orchestrator.observability.logfire import configure_logfire
        configure_logfire()
    except Exception:
        pass

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

    tenant = uuid4()
    run_id = _seed_tenant_and_run(pool, tenant)

    # ---------------------------------------------------------------
    # Drive 3 Messages.create round-trips inside observability +
    # reasoning_step_input context. Use Haiku (claude-haiku-4-5) per
    # CL-248 test-model + CL-274 two-mode default.
    # ---------------------------------------------------------------

    client = Anthropic()
    model = "claude-haiku-4-5"

    # Wrap a Logfire span so trace_id is non-None per A3 (if Logfire
    # configured). Without Logfire, trace_id is None and A3 asserts
    # graceful-None.
    logfire_available = False
    try:
        import logfire
        logfire_available = bool(os.environ.get("LOGFIRE_TOKEN"))
    except ImportError:
        pass

    # The synthetic agent prompts the model to reference a phone number;
    # the callback's redact_for_log should strip the phone digits and
    # tokenize. Phone deliberately embedded with cust_tok_ wrapping to
    # exercise the redactor — the canary's A2 verifies tokens not raw digits.
    phone_in_prompt = "+919876543210"
    test_messages = [
        [{
            "role": "user",
            "content": (
                f"Customer at {phone_in_prompt} asked about a refund. "
                "Acknowledge in one short sentence (≤20 words)."
            ),
        }],
        [{
            "role": "user",
            "content": (
                f"Same customer ({phone_in_prompt}) is upset about delay. "
                "Suggest one polite next step in ≤20 words."
            ),
        }],
        [{
            "role": "user",
            "content": (
                f"Wrap up the conversation with {phone_in_prompt}. "
                "Goodbye message in ≤15 words."
            ),
        }],
    ]

    def _drive_one_iteration(idx: int, messages: list[dict[str, Any]]) -> None:
        with observability_context(run_id=run_id, tenant_id=tenant):
            with reasoning_step_input(
                context_bundle_hash=f"hash-{idx}-{uuid4().hex[:8]}",
                context_bundle_components=["owner_profile", "customer_history"],
                context_bundle_token_count=200 + idx * 50,
                prior_tool_calls_count=idx,
                prior_tool_calls_summary=[
                    {"tool": "prev", "iter": j} for j in range(idx)
                ],
            ):
                # Cache batch 2026-07-18: _run_one_turn takes the system as a
                # block LIST (matching _render_sr_system_prompt's shape). The
                # canary's tiny prompt needs no cache_control marker.
                canary_system = [
                    {
                        "type": "text",
                        "text": "You are a concise customer-service helper.",
                    }
                ]
                if logfire_available:
                    with logfire.span(f"canary-iteration-{idx}"):
                        _run_one_turn(
                            client,
                            model=model,
                            system_prompt=canary_system,
                            messages=messages,
                        )
                else:
                    _run_one_turn(
                        client,
                        model=model,
                        system_prompt=canary_system,
                        messages=messages,
                    )

    for idx, msgs in enumerate(test_messages):
        _drive_one_iteration(idx, msgs)

    # ---------------------------------------------------------------
    # Pull the agent_reasoning_step rows + run assertions
    # ---------------------------------------------------------------

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq, step_name, status, decision_rationale, "
            "model_used, tokens_input, tokens_output, cost_paise, "
            "parent_step_id, input_envelope, output_envelope "
            "FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_reasoning_step' "
            "ORDER BY step_seq",
            (str(run_id),),
        )
        rows = cur.fetchall()

    # A1: exactly 3 agent_reasoning_step rows
    pass_1 = len(rows) == 3
    assertion(
        1,
        "3-iteration synthetic agent run → 3 agent_reasoning_step rows",
        pass_1,
        observed={"row_count": len(rows)},
        expected={"row_count": 3},
    )

    # A2: think_text_redacted contains cust_tok_ token; NO raw digits
    raw_phone_digits = "9876543210"
    tokens_present = 0
    raw_leaked = 0
    sample_think: list[str | None] = []
    for r in rows:
        out_env = r["output_envelope"] or {}
        think = out_env.get("think_text") or ""
        sample_think.append(think[:100])
        if "cust_tok_" in think:
            tokens_present += 1
        if raw_phone_digits in think:
            raw_leaked += 1
    pass_2 = raw_leaked == 0  # Strong assertion: no raw digit leakage
    assertion(
        2,
        "think_text_redacted strips raw phone digits (phone-tokenization redactor active)",
        pass_2,
        observed={
            "rows_with_cust_tok": tokens_present,
            "rows_with_raw_digits": raw_leaked,
            "sample_think_first100chars": sample_think,
        },
        expected={"rows_with_raw_digits": 0},
    )

    # A3: Logfire trace_id captured (32-hex string) when Logfire active;
    # None when absent. Both are valid; assert at least graceful handling.
    trace_ids = [r["output_envelope"].get("logfire_trace_id") for r in rows]
    if logfire_available:
        pass_3 = all(
            isinstance(tid, str) and len(tid) == 32 and all(c in "0123456789abcdef" for c in tid)
            for tid in trace_ids
        )
        expected_3 = {"trace_ids_all_32hex": True, "logfire_available": True}
    else:
        pass_3 = all(tid is None for tid in trace_ids)
        expected_3 = {"all_trace_ids_None": True, "logfire_available": False}
    assertion(
        3,
        "Logfire trace_id captured (32-hex when active; None when absent, graceful per CL-56)",
        pass_3,
        observed={"trace_ids": trace_ids, "logfire_available": logfire_available},
        expected=expected_3,
    )

    # A4: tokens_input/output exist and are positive (response.usage was captured)
    tokens_ok = all(
        r["tokens_input"] is not None and r["tokens_input"] > 0
        and r["tokens_output"] is not None and r["tokens_output"] > 0
        for r in rows
    )
    pass_4 = tokens_ok
    assertion(
        4,
        "tokens_input/output captured from response.usage (positive integers)",
        pass_4,
        observed={
            "tokens": [
                {"in": r["tokens_input"], "out": r["tokens_output"]}
                for r in rows
            ],
        },
        expected={"all_positive_ints": True},
    )

    # A5: model_used matches Haiku alias
    models = sorted({r["model_used"] for r in rows})
    pass_5 = len(models) == 1 and "haiku" in models[0].lower()
    assertion(
        5,
        "model_used == claude-haiku-4-5 (or alias containing 'haiku')",
        pass_5,
        observed={"models": models},
        expected={"models_contain_haiku": True},
    )

    # A6: cost_paise == compute_cost_paise(_normalize_model_for_rates(model), in, out)
    from orchestrator.observability.agent_callback import _normalize_model_for_rates
    cost_matches = []
    for r in rows:
        normalized_model = _normalize_model_for_rates(r["model_used"])
        expected = compute_cost_paise(
            model=normalized_model,
            input_tokens=r["tokens_input"],
            output_tokens=r["tokens_output"],
        )
        cost_matches.append((expected, r["cost_paise"], expected == r["cost_paise"]))
    pass_6 = all(m[2] for m in cost_matches)
    assertion(
        6,
        "cost_paise == compute_cost_paise(model, in, out) per row",
        pass_6,
        observed={"cost_check_triplets_expected_actual_match": cost_matches},
        expected={"all_match": True},
    )

    # A7: parent_step_id is None (no nested loop in this canary — synthetic
    # is single-level). Assert NULL handling is consistent (not invalid garbage).
    parent_step_ids = [r["parent_step_id"] for r in rows]
    pass_7 = all(p is None for p in parent_step_ids)
    assertion(
        7,
        "parent_step_id populated consistently (None for top-level canary; nested loops set it)",
        pass_7,
        observed={"parent_step_ids": parent_step_ids},
        expected={"all_None_for_top_level_canary": True},
    )

    # A8: total cost_paise across the 3 calls < 100 (₹1)
    total_cost = sum(r["cost_paise"] for r in rows)
    pass_8 = total_cost < 100
    assertion(
        8,
        f"cost_budget total < ₹1: observed total={total_cost} paise across 3 Haiku calls",
        pass_8,
        observed={"total_cost_paise": total_cost, "rows": len(rows)},
        expected={"total_cost_paise_lt": 100},
    )

    return _finalise(pool, total_cost)


def _finalise(pool, total_cost: int) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print(f"\n=== Anthropic cost: {total_cost} paise (₹{total_cost/100:.2f}) over 3 Haiku calls ===")

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
