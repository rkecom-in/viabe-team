#!/usr/bin/env python3
"""VT-303 brain owner_inputs consent-gate canary (Rule #15, DR-15).

Proves, against a REAL Postgres on the live call chain, the Option-B gate that
runner.webhook_pipeline_run applies before transmitting an owner's inbound body
to Anthropic (CL-425: owner_inputs is the lawful basis). The full ingress→brain
E2E is covered in tests/orchestrator/test_twilio_ingress.py; this canary
exercises the gate-decision fn + the enable setter + cross-tenant + fail-closed.

- A1: owner_inputs=False → _brain_owner_inputs_ok False (gate would NOT transmit)
- A2: pre_filter("ACTIVATE TEAM") → routes to data_inputs_enable_handler
- A3: data_inputs_enable_handler → tenants.owner_inputs flips TRUE (DB verified)
- A4: after enable → _brain_owner_inputs_ok True (gate would transmit)
- A5: cross-tenant — a second tenant (never enabled) stays False
- A6: fail-closed — unknown tenant id → _brain_owner_inputs_ok False

Subshell-source supabase-dev.env (see vt196 for the pattern):

    cd apps/team-orchestrator
    ( set -a; source ../../.viabe/secrets/supabase-dev.env; set +a;
      ./.venv/bin/python canaries/vt303_brain_consent_gate.py )
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    # No ANTHROPIC_API_KEY needed — the gate's FALSE path never calls the brain.
    print("PREFLIGHT OK")


def _seed_tenant(pool: Any, owner_inputs: bool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, "
            "whatsapp_number, owner_inputs) "
            "VALUES (%s, %s, 'standard', 'trial', %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tid, f"vt303-{tid[:8]}", f"+9199{uuid4().hex[:8]}", owner_inputs),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _cleanup(pool: Any) -> None:
    with pool.connection() as conn:
        for tid in INSERTED_TENANTS:
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod

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
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", os.environ["DATABASE_URL"])
    pool = graph_mod.get_pool()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        import orchestrator.runner as runner_mod
        from orchestrator.direct_handlers import HANDLERS
        from orchestrator.pre_filter_gate import pre_filter
        from orchestrator.state import new_subscriber_state
        from orchestrator.types import RouteToDirectHandler, WebhookEvent

        def _event(body: str) -> WebhookEvent:
            return WebhookEvent(body=body, sender_phone="+910000000000")

        from uuid import UUID

        # A1 — no consent → gate would NOT transmit.
        tid = _seed_tenant(pool, owner_inputs=False)
        ok_false = runner_mod._brain_owner_inputs_ok(tid)
        assertion(1, "owner_inputs=False → _brain_owner_inputs_ok False",
                  ok_false is False, observed=ok_false)

        # A2 — enable phrase routes to the enable handler.
        state = new_subscriber_state(UUID(tid))
        routed = pre_filter(_event("ACTIVATE TEAM"), state)
        a2 = isinstance(routed, RouteToDirectHandler) and (
            routed.handler_name == "data_inputs_enable_handler"
        )
        assertion(2, "pre_filter('ACTIVATE TEAM') → data_inputs_enable_handler",
                  a2, observed=getattr(routed, "handler_name", routed))

        # A3 — enable handler flips owner_inputs TRUE.
        HANDLERS["data_inputs_enable_handler"](_event("ACTIVATE TEAM"), state)
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT owner_inputs FROM tenants WHERE id = %s", (tid,)
            ).fetchone()
        flipped = bool(row["owner_inputs"] if isinstance(row, dict) else row[0])
        assertion(3, "data_inputs_enable_handler → owner_inputs TRUE",
                  flipped is True, observed=row)

        # A4 — after enable, gate would transmit.
        ok_true = runner_mod._brain_owner_inputs_ok(tid)
        assertion(4, "after enable → _brain_owner_inputs_ok True",
                  ok_true is True, observed=ok_true)

        # A5 — cross-tenant: a second tenant never enabled stays False.
        tid_b = _seed_tenant(pool, owner_inputs=False)
        ok_b = runner_mod._brain_owner_inputs_ok(tid_b)
        assertion(5, "cross-tenant: unrelated tenant stays False",
                  ok_b is False, observed=ok_b)

        # A6 — fail-closed on unknown tenant id.
        ok_unknown = runner_mod._brain_owner_inputs_ok(str(uuid4()))
        assertion(6, "unknown tenant id → fail-closed False",
                  ok_unknown is False, observed=ok_unknown)

        _cleanup(pool)
    finally:
        shutdown_dbos()

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
