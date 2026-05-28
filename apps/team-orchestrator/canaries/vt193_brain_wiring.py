#!/usr/bin/env python3
"""VT-193 brain-wiring canary (Rule #15, DR-15).

Proves the runner.py:303-307 placeholder is replaced by a real
``dispatch_brain`` call: synthetic Twilio webhook with substantive
English body → pre_filter routes to brain → ``dispatch_brain`` invokes
``build_supervisor_graph(model)`` under ``observability_context`` →
real Anthropic call → ``agent_reasoning_step`` rows written +
``compose_output`` envelope written + run closes ``status='completed'``.

Subshell-source supabase-dev.env + anthropic.env. NO orchestrator
boot needed by this script — assumes ``http://localhost:8001`` is
already running with both envs sourced (so the workflow path sees
Anthropic + Supabase + JWT_SECRET).

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/anthropic.env
      set +a
      time ./.venv/bin/python canaries/vt193_brain_wiring.py 2>&1 | tee /tmp/vt193-canary-evidence.log | tail -200
    )

Wall-clock budget ≤ 60s. Anthropic cost budget ≤ 50 paise.

6 assertions:

- A1: substantive English body → pre_filter brain → supervisor →
  pipeline_runs.status='completed' (NOT 'escalated' / NOT
  'aborted_hard_limit')
- A2: ≥1 ``agent_reasoning_step`` row in pipeline_steps with
  cost_paise > 0 + model_used set (Anthropic call landed)
- A3: exactly 1 ``agent_invocation`` row (the dispatch ENTRY
  envelope — VT-179 canonical kind reused per Cowork brief
  correction; replaces the brain_dispatch kind that was originally
  in the plan-ready)
- A4: exactly 1 ``compose_output`` row carrying the unified-output
  payload (template_name / content_sid present on output_envelope OR
  ``body_preview`` non-null)
- A5: hard-limit termination (VT-199 refactor) — in-process call to
  ``dispatch_brain()`` with ``_NullDriver.cost_limit_paise = 0`` patched
  so any non-zero usage trips immediately on first Anthropic response.
  Deterministic trip; restored in try/finally. Asserts
  ``DispatchResult.final_status == 'aborted_hard_limit'`` +
  ``aborted_hard_limit`` envelope row written (Pillar 8 clean-
  termination contract, no DBOS retry).
- A6: wall-clock < 60s; total Anthropic cost < 1000 paise (Opus 4.7
  baseline; revised from <50 per VT-199 brief — post-VT-194 prompt
  caching could tighten further but tightening = own row).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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


def _preflight() -> str:
    required = ("DATABASE_URL", "ANTHROPIC_API_KEY", "INTERNAL_API_SECRET")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)

    import httpx

    orch_base = os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8001")
    try:
        httpx.get(orch_base, timeout=3.0)
    except httpx.HTTPError as exc:
        print(
            f"PREFLIGHT FAIL — orchestrator unreachable at {orch_base}: {exc!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — orchestrator: {orch_base}; "
        f"ANTHROPIC_API_KEY: present (real Anthropic call mode)"
    )
    return orch_base


def _seed_tenant(pool: Any, tenant_phone: str) -> str:
    from uuid import uuid4 as _u4

    tenant_id = _u4()
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"vt193 canary {tenant_id.hex[:6]}", tenant_phone),
        )
    return str(tenant_id)


def _fire_webhook(orch_base: str, tenant_phone: str, body: str) -> str:
    """POST a synthetic webhook + return the derived run_id (UUID5)."""
    import httpx

    message_sid = f"SM{uuid4().hex}"
    run_id = uuid5(NAMESPACE_URL, message_sid)
    twilio_fields = {
        "From": tenant_phone,
        "To": "+910000000000",
        "Body": body,
        "MessageSid": message_sid,
        "NumMedia": "0",
    }
    res = httpx.post(
        f"{orch_base}/api/orchestrator/twilio-ingress",
        json={"twilio_fields": twilio_fields},
        headers={"X-Internal-Secret": os.environ["INTERNAL_API_SECRET"]},
        timeout=15.0,
    )
    if res.status_code != 200:
        raise RuntimeError(f"webhook POST failed: HTTP {res.status_code} {res.text}")
    INSERTED_RUN_IDS.append(str(run_id))
    return str(run_id)


def _wait_for_terminal(pool: Any, run_id: str, max_wait_s: float = 40.0) -> str | None:
    """Poll pipeline_runs until status terminal or timeout."""
    poll_start = time.monotonic()
    while time.monotonic() - poll_start < max_wait_s:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM pipeline_runs WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if row and row["status"] in (
            "completed", "failed", "terminal", "escalated", "aborted_hard_limit"
        ):
            return row["status"]
        time.sleep(0.5)
    return None


def run_canary() -> int:
    t_start = time.monotonic()
    orch_base = _preflight()

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

    # ---------------- Happy path: A1-A4 + A6 ----------------
    happy_phone = f"+9199888{uuid4().hex[:6]}"
    _seed_tenant(pool, happy_phone)
    happy_body = (
        "can you give me a quick summary of how my restaurant is doing this week"
    )
    happy_run_id = _fire_webhook(orch_base, happy_phone, happy_body)
    happy_status = _wait_for_terminal(pool, happy_run_id, max_wait_s=45.0)

    # A1
    pass_1 = happy_status == "completed"
    assertion(
        1,
        "substantive English → brain → supervisor → status='completed'",
        pass_1,
        observed={"status": happy_status, "run_id": happy_run_id},
        expected={"status": "completed"},
    )

    # Read pipeline_steps for the happy run
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_kind, step_name, cost_paise, model_used, "
            "input_envelope, output_envelope "
            "FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (happy_run_id,),
        )
        steps = cur.fetchall()

    step_kinds = [s["step_kind"] for s in steps]
    reasoning_rows = [
        s for s in steps
        if s["step_kind"] == "agent_reasoning_step"
        and (s["cost_paise"] or 0) > 0
        and s["model_used"]
    ]
    # A2
    pass_2 = len(reasoning_rows) >= 1
    assertion(
        2,
        "≥1 agent_reasoning_step row with cost_paise > 0 + model_used set",
        pass_2,
        observed={
            "reasoning_row_count": len(reasoning_rows),
            "first_model": reasoning_rows[0]["model_used"] if reasoning_rows else None,
            "step_kinds": step_kinds,
        },
        expected={"reasoning_row_count_gte": 1},
    )

    # A3 — exactly 1 agent_invocation row (dispatch entry)
    agent_invocation_rows = [s for s in steps if s["step_kind"] == "agent_invocation"]
    pass_3 = len(agent_invocation_rows) == 1
    assertion(
        3,
        "exactly 1 agent_invocation row (dispatch ENTRY envelope)",
        pass_3,
        observed={"agent_invocation_count": len(agent_invocation_rows)},
        expected={"agent_invocation_count": 1},
    )

    # A4 — exactly 1 compose_output row + payload non-empty signal
    compose_rows = [s for s in steps if s["step_kind"] == "compose_output"]
    if compose_rows:
        co = compose_rows[0]
        env = co.get("output_envelope") or {}
        payload_signal = bool(
            (env.get("template_name") if isinstance(env, dict) else None)
            or (env.get("body_preview") if isinstance(env, dict) else None)
            or (env.get("content_sid") if isinstance(env, dict) else None)
        )
    else:
        payload_signal = False
    pass_4 = len(compose_rows) == 1 and payload_signal
    assertion(
        4,
        "exactly 1 compose_output row with non-empty unified-output payload",
        pass_4,
        observed={
            "compose_output_count": len(compose_rows),
            "payload_signal": payload_signal,
        },
        expected={"compose_output_count": 1, "payload_signal": True},
    )

    # ---------------- A5 — hard-limit termination (VT-199 refactor) ----------------
    # Deterministic in-process trip. We bypass the HTTP/DBOS path entirely and
    # call ``dispatch_brain()`` directly with a synthesized WebhookEvent +
    # SubscriberState. With ``_NullDriver.cost_limit_paise = 0``, the first
    # Anthropic response (any non-zero cost) trips ``HardLimitExceeded`` inside
    # the callback's ``check_mid_invocation``; ``dispatch_brain`` then closes
    # the run with ``final_status='aborted_hard_limit'`` and writes the
    # envelope. Class attribute restored in finally so subsequent canaries
    # (and future re-runs of this canary in the same pytest process) see the
    # original 500 paise limit.
    from uuid import uuid4 as _u4

    from orchestrator.agent.dispatch import _NullDriver as NullDriver
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    hl_phone = f"+9199777{_u4().hex[:6]}"
    hl_tenant_id = _seed_tenant(pool, hl_phone)
    hl_run_id = str(_u4())
    INSERTED_RUN_IDS.append(hl_run_id)
    # Pre-INSERT pipeline_runs row so ``dispatch_brain``'s envelope writes
    # (which FK to pipeline_runs.id via pipeline_steps) succeed.
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, trigger_kind) "
            "VALUES (%s, %s, 'running', 'inbound_whatsapp')",
            (hl_run_id, hl_tenant_id),
        )

    hl_event = WebhookEvent(
        body="please walk me through how my restaurant performed last week",
        sender_phone=hl_phone,
        twilio_message_sid=f"SM{_u4().hex}",
    )
    from uuid import UUID

    hl_state = new_subscriber_state(
        tenant_id=UUID(hl_tenant_id), run_id=UUID(hl_run_id), phase="paid_active"
    )

    original_cost_limit = NullDriver.cost_limit_paise
    NullDriver.cost_limit_paise = 0
    try:
        dispatch_result = dispatch_brain(
            event=hl_event,
            state=hl_state,
            run_id=UUID(hl_run_id),
            tenant_id=UUID(hl_tenant_id),
        )
    finally:
        NullDriver.cost_limit_paise = original_cost_limit

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'aborted_hard_limit'",
            (hl_run_id,),
        )
        ahl_row = cur.fetchone()
    ahl_count = int(ahl_row["n"]) if ahl_row else 0
    pass_5 = (
        dispatch_result.final_status == "aborted_hard_limit"
        and ahl_count == 1
    )
    assertion(
        5,
        "in-process hard-limit trip: cost_limit_paise=0 → final_status='aborted_hard_limit' + envelope",
        pass_5,
        observed={
            "final_status": dispatch_result.final_status,
            "terminal_path": dispatch_result.terminal_path,
            "reason": dispatch_result.reason,
            "aborted_hard_limit_rows": ahl_count,
        },
        expected={"final_status": "aborted_hard_limit", "aborted_hard_limit_rows": 1},
    )

    # ---------------- A6 — budgets (VT-199 revision) ----------------
    # Tightened from <5000 paise to <1000 paise per VT-199 brief AC-2.
    # Observed baseline ~895 paise per VT-194 cost analysis. VT-194 prompt
    # caching is now landed (PR #86) — post-caching baseline would allow
    # further tightening to ~200 paise, but VT-199 brief explicitly says
    # do NOT tighten beyond <1000 in this row; tightening = separate row.
    # Wall-clock <60s retained per brief; A5 in-process invocation keeps
    # total under one happy-path + one in-process dispatch_brain call.
    total_elapsed = time.monotonic() - t_start
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(total_cost_paise), 0) AS total_cost "
            "FROM pipeline_runs WHERE id = ANY(%s)",
            (INSERTED_RUN_IDS,),
        )
        cost_row = cur.fetchone()
    total_cost = int(cost_row["total_cost"]) if cost_row else 0
    pass_6 = total_elapsed < 60.0 and total_cost < 1000
    assertion(
        6,
        "wall-clock < 60s AND total Anthropic cost < 1000 paise (Opus 4.7 baseline)",
        pass_6,
        observed={"elapsed_s": round(total_elapsed, 2), "total_cost_paise": total_cost},
        expected={"elapsed_s_lt": 60.0, "total_cost_paise_lt": 1000},
    )

    return _finalise(pool, t_start)


def _finalise(pool: Any, t_start: float) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    total = time.monotonic() - t_start
    print(f"\n=== Total wall-clock: {total:.1f}s ===")
    print("=== Anthropic cost budget: < 1000 paise (Opus 4.7 baseline; VT-199) ===")

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
                cur.execute(
                    "DELETE FROM pipeline_log WHERE run_id = ANY(%s)",
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
    print(f"\nALL {len(RESULTS)} ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
