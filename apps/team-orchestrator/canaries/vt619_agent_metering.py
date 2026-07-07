#!/usr/bin/env python3
"""VT-619 per-tenant × per-agent metering + limits canary (Rule #15, DR-15).

Proves the metering seams + the hard-cap enforcement gate against a DEPLOYED orchestrator, using
BOGUS fixture tenants only (synthetic owner numbers — never a real/provided number; no real
customer send is ever driven — A3's send check is a GATE evaluation that returns allowed=False).

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/anthropic.env
      set +a
      ./.venv/bin/python canaries/vt619_agent_metering.py 2>&1 | tee /tmp/vt619-canary.log | tail -200
    )

Assertions:

- A1: ONE manager turn that does NOT spawn → exactly ONE tenant_agent_usage row for the tenant's
      fallback agent ('sales_recovery'), api_calls >= 1, tokens_in/out > 0. (langchain seam,
      fallback attribution.)
- A2: a turn that runs the sales_recovery executor → sales_recovery.api_calls == the NUMBER of
      agent_reasoning_step rows for the run (EXACTLY-ONCE arbiter: a double-count would make
      api_calls > the reasoning-step count). Also reports whether the SR executor actually ran
      (an 'agent_turn' step present) so a no-spawn run is not mistaken for a passing double-count
      guard.
- A3: seed a sales_recovery usage row at >=100% of cap → (a) assert_customer_send_allowed returns
      allowed=False reason=SKIP_BUDGET_EXHAUSTED, AND (b) a plain manager status_query still
      reaches a completed terminal (the hard pause blocks agent ACTIONS, not the conversation).

Read DATABASE_URL / ANTHROPIC_API_KEY / INTERNAL_API_SECRET from env. Prints only boolean/aggregate
assertions (never a secret or a phone). Runnable but NOT run here (needs a live LLM key) — validate
on dev.
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
        print(f"PREFLIGHT FAIL — orchestrator unreachable at {orch_base}: {exc!r}", file=sys.stderr)
        sys.exit(2)
    print(f"PREFLIGHT OK — orchestrator: {orch_base}; ANTHROPIC_API_KEY present (real-call mode)")
    return orch_base


def _seed_tenant(pool: Any, tenant_phone: str) -> str:
    tenant_id = uuid4()
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', %s) ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"vt619 canary {tenant_id.hex[:6]}", tenant_phone),
        )
    return str(tenant_id)


def _fire_webhook(orch_base: str, tenant_phone: str, body: str) -> str:
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


def _wait_for_terminal(pool: Any, run_id: str, max_wait_s: float = 60.0) -> str | None:
    poll_start = time.monotonic()
    while time.monotonic() - poll_start < max_wait_s:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM pipeline_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
        if row and row["status"] in (
            "completed", "failed", "terminal", "escalated", "aborted_hard_limit"
        ):
            return row["status"]
        time.sleep(0.5)
    return None


def _usage_rows(tenant_id: str) -> list[dict[str, Any]]:
    """Current-month usage rows for a tenant, read under RLS (tenant_connection)."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT agent, api_calls, tokens_in, tokens_out, "
                "       soft_notified_at, hard_notified_at "
                "FROM tenant_agent_usage "
                "WHERE tenant_id = %s AND period_month = date_trunc('month', now())::date",
                (tenant_id,),
            ).fetchall()
        ]


def _agent_row(tenant_id: str, agent: str) -> dict[str, Any] | None:
    return next((r for r in _usage_rows(tenant_id) if r["agent"] == agent), None)


def _reasoning_steps(pool: Any, run_id: str) -> list[str]:
    """The step_name of every agent_reasoning_step row for a run (raw pool, like vt194)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_name FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'agent_reasoning_step' ORDER BY step_seq",
            (run_id,),
        )
        return [r["step_name"] for r in cur.fetchall()]


def _cap_api_calls(tenant_id: str, agent: str) -> int:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as c:
        row = c.execute(
            "SELECT max_api_calls FROM agent_cost_limits WHERE agent = %s", (agent,)
        ).fetchone()
        if row is None:
            row = c.execute(
                "SELECT max_api_calls FROM agent_cost_limits WHERE agent = 'DEFAULT'"
            ).fetchone()
    return int(row["max_api_calls"]) if row else 4000


def _seed_over_cap(tenant_id: str, agent: str) -> None:
    """Dev-only: force ``agent`` to >=100% of its api_calls cap for the current month (RLS path)."""
    from orchestrator.db import tenant_connection

    cap = _cap_api_calls(tenant_id, agent)
    with tenant_connection(tenant_id) as c:
        c.execute(
            "INSERT INTO tenant_agent_usage "
            "  (tenant_id, agent, period_month, api_calls, tokens_in, tokens_out) "
            "VALUES (%s, %s, date_trunc('month', now())::date, %s, 0, 0) "
            "ON CONFLICT (tenant_id, agent, period_month) "
            "DO UPDATE SET api_calls = EXCLUDED.api_calls",
            (tenant_id, agent, cap),
        )


def _make_pool() -> Any:
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
    return get_pool()


def run_canary() -> int:
    t_start = time.monotonic()
    orch_base = _preflight()
    pool = _make_pool()

    # ---------------- A1: manager no-spawn turn → fallback agent metered ----------------
    phone_1 = f"+9199888{uuid4().hex[:6]}"
    tenant_1 = _seed_tenant(pool, phone_1)
    run_1 = _fire_webhook(
        orch_base, phone_1, "can you give me a quick summary of how my business is doing"
    )
    status_1 = _wait_for_terminal(pool, run_1)
    rows_1 = _usage_rows(tenant_1)
    sr_1 = _agent_row(tenant_1, "sales_recovery")
    pass_1 = (
        len(rows_1) == 1
        and sr_1 is not None
        and int(sr_1["api_calls"]) >= 1
        and int(sr_1["tokens_in"]) > 0
        and int(sr_1["tokens_out"]) > 0
    )
    assertion(
        1,
        "no-spawn manager turn → exactly one 'sales_recovery' usage row, counters > 0",
        pass_1,
        observed={
            "row_count": len(rows_1),
            "agents": [r["agent"] for r in rows_1],
            "sales_recovery": sr_1,
            "run_status": status_1,
        },
        expected={"row_count": 1, "agent": "sales_recovery", "api_calls_ge": 1, "tokens_gt": 0},
    )

    # ---------------- A2: SR executor turn → EXACTLY-ONCE (no double-count) ----------------
    phone_2 = f"+9199888{uuid4().hex[:6]}"
    tenant_2 = _seed_tenant(pool, phone_2)
    run_2 = _fire_webhook(
        orch_base, phone_2, "please help me win back my dormant customers with a campaign"
    )
    status_2 = _wait_for_terminal(pool, run_2)
    steps_2 = _reasoning_steps(pool, run_2)
    sr_2 = _agent_row(tenant_2, "sales_recovery")
    sr_calls_2 = int(sr_2["api_calls"]) if sr_2 else 0
    executor_ran = any(s == "agent_turn" for s in steps_2)  # SR Messages-SDK seam step_name
    # EXACTLY-ONCE: every metered LLM call also writes ONE agent_reasoning_step (both seams do so
    # in the same block), so api_calls == reasoning-step count. A double-count would make it larger.
    pass_2 = sr_calls_2 >= 1 and sr_calls_2 == len(steps_2)
    assertion(
        2,
        "SR-spawn run: sales_recovery.api_calls == agent_reasoning_step count (exactly-once)",
        pass_2,
        observed={
            "sr_api_calls": sr_calls_2,
            "reasoning_step_count": len(steps_2),
            "step_names": steps_2,
            "sr_executor_ran": executor_ran,
            "run_status": status_2,
        },
        expected={"api_calls_eq_step_count": True, "not_double": "api_calls == steps, not 2x"},
    )
    if not executor_ran:
        print(
            "    NOTE: no 'agent_turn' (SR executor) step — the run did not spawn SR, so A2's "
            "double-count arbiter is weaker. Re-run if this recurs.",
            file=sys.stderr,
        )

    # ---------------- A3: hard cap blocks SEND action, not the conversation ----------------
    phone_3 = f"+9199888{uuid4().hex[:6]}"
    tenant_3 = _seed_tenant(pool, phone_3)
    _seed_over_cap(tenant_3, "sales_recovery")

    from orchestrator.agents.customer_send_choke import (
        SKIP_BUDGET_EXHAUSTED,
        assert_customer_send_allowed,
    )
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_3) as c:
        gate = assert_customer_send_allowed(tenant_3, agent="sales_recovery", conn=c)
    pass_3a = gate.allowed is False and gate.reason == SKIP_BUDGET_EXHAUSTED
    assertion(
        3,
        "over-hard-cap: assert_customer_send_allowed blocks with SKIP_BUDGET_EXHAUSTED",
        pass_3a,
        observed={"allowed": gate.allowed, "reason": gate.reason},
        expected={"allowed": False, "reason": SKIP_BUDGET_EXHAUSTED},
    )

    # The conversational turn must still ANSWER (hard pause blocks agent actions, not the chat).
    run_3 = _fire_webhook(orch_base, phone_3, "what is the status of my account")
    status_3 = _wait_for_terminal(pool, run_3)
    hard_stamped = bool((_agent_row(tenant_3, "sales_recovery") or {}).get("hard_notified_at"))
    pass_4 = status_3 == "completed"
    assertion(
        4,
        "over-hard-cap: a plain status_query still reaches a completed terminal (chat answers)",
        pass_4,
        observed={"run_status": status_3, "hard_notified_stamped": hard_stamped},
        expected={"run_status": "completed"},
    )

    return _finalise(pool, t_start)


def _finalise(pool: Any, t_start: float) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    print(f"\n=== Total wall-clock: {time.monotonic() - t_start:.1f}s ===")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if INSERTED_RUN_IDS:
                cur.execute("DELETE FROM pipeline_steps WHERE run_id = ANY(%s)", (INSERTED_RUN_IDS,))
                cur.execute("DELETE FROM pipeline_runs WHERE id = ANY(%s)", (INSERTED_RUN_IDS,))
            if INSERTED_TENANT_IDS:
                # tenant_agent_usage + incidents CASCADE on tenant delete, but clear explicitly too.
                cur.execute(
                    "DELETE FROM tenant_agent_usage WHERE tenant_id = ANY(%s)", (INSERTED_TENANT_IDS,)
                )
                cur.execute(
                    "DELETE FROM incidents WHERE tenant_id = ANY(%s)", (INSERTED_TENANT_IDS,)
                )
                cur.execute(
                    "DELETE FROM twilio_inbound_events WHERE tenant_id = ANY(%s)",
                    (INSERTED_TENANT_IDS,),
                )
                cur.execute("DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,))
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
