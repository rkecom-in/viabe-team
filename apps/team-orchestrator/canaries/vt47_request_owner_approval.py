#!/usr/bin/env python3
"""VT-47 request_owner_approval pause/resume canary (Rule #15, DR-15).

Real pause->resume cycle on a SEEDED SYNTHETIC run (CL-422), against a live
Postgres + the real PostgresSaver checkpointer. NO live Twilio (dry-run send),
NO live Anthropic (the resume decision is supplied directly / the timeout verb
is fixed — the gate node never calls the LLM).

Subshell-source the dev Supabase DSN:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt47_request_owner_approval.py 2>&1 \
        | tee /tmp/vt47-canary-evidence.log | tail -120
    )

Local pg16 equivalent:

    DATABASE_URL="postgresql:///viabe_vt47_test?host=/tmp&port=5432" \
      ./.venv/bin/python canaries/vt47_request_owner_approval.py

7 assertions:
  A1 — pause: graph.invoke surfaces __interrupt__ (the run halted).
  A2 — pending_approvals row exists with decision NULL (the durable pause).
  A3 — resume(approved): node returns owner_decision='approved'.
  A4 — idempotency: exactly ONE approval row after resume (no duplicate from
       the node's resume re-execution).
  A5 — Pillar-7 cannot-bypass: a SECOND seeded run left UNRESOLVED never
       yields owner_decision='approved' (the gate is authoritative).
  A6 — timeout path: a past-timeout open approval, swept, lands
       decision='timeout' + status='timed_out' + resolved.
  A7 — CL-390: no PII (phone / message body) anywhere we logged.

Fail-not-skip: any failure -> non-zero exit. CL-422 SYNTHETIC tenant only.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import psycopg  # noqa: E402

RESULTS: dict[int, dict[str, Any]] = {}
SEEDED_TENANTS: list[str] = []
SEEDED_RUNS: list[str] = []


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL", "")


def _preflight() -> None:
    if not _dsn():
        print("PREFLIGHT FAIL — set DATABASE_URL or TEAM_SUPABASE_DB_URL", file=sys.stderr)
        sys.exit(2)
    # No ANTHROPIC / TWILIO env required — the gate node dry-runs the send and
    # the resume decision is supplied directly (no classifier call).
    print(
        "PREFLIGHT OK — postgres DSN present; twilio: DRY-RUN (no send); "
        "anthropic: NOT CALLED (decision supplied directly / timeout fixed)"
    )


def _seed_run(dsn: str, *, timeout_hours: int = 48) -> tuple[str, str]:
    """Seed a synthetic tenant + a running pipeline_run. Returns (tenant, run)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        tid = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, owner_phone) "
            "VALUES ('VT47 Canary', 'founding', 'onboarding', %s) RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()[0]
        rid = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'running') RETURNING id",
            (tid,),
        ).fetchone()[0]
    SEEDED_TENANTS.append(str(tid))
    SEEDED_RUNS.append(str(rid))
    return str(tid), str(rid)


def _request(tid: str, rid: str) -> dict[str, Any]:
    return {
        "tenant_id": UUID(tid),
        "run_id": UUID(rid),
        "pending_approval_request": {
            "approval_type": "campaign_send",
            "summary": "Approve send to 3 customers?",
            "details": {"cohort_size": 3},
            "template_params": {},
            "dry_run": True,
            "timeout_hours": 48,
        },
    }


def _build_gate_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph

    from orchestrator.agent.tools.request_owner_approval import (
        request_owner_approval_node,
    )
    from orchestrator.state.agent_graph_state import AgentGraphState

    g = StateGraph(AgentGraphState)
    g.add_node("gate", request_owner_approval_node)
    g.add_edge(START, "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=checkpointer)


def _cleanup(dsn: str) -> None:
    """Remove the synthetic rows this canary seeded, in dependency order.

    pipeline_runs.tenant_id FK is NOT ON DELETE CASCADE, and the LangGraph
    checkpoint tables reference run_ids (thread_id). Delete: checkpoints ->
    pending_approvals -> pipeline_runs -> tenants. pending_approvals CASCADEs
    from pipeline_runs, but we delete explicitly to be order-robust.
    """
    if not SEEDED_TENANTS:
        return
    with psycopg.connect(dsn, autocommit=True) as conn:
        if SEEDED_RUNS:
            for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                try:
                    conn.execute(
                        f"DELETE FROM {tbl} WHERE thread_id = ANY(%s)",
                        (SEEDED_RUNS,),
                    )
                except psycopg.Error:
                    pass  # checkpoint table layout may vary; best-effort cleanup
            conn.execute(
                "DELETE FROM pending_approvals WHERE run_id = ANY(%s::uuid[])",
                (SEEDED_RUNS,),
            )
            conn.execute(
                "DELETE FROM pipeline_runs WHERE id = ANY(%s::uuid[])",
                (SEEDED_RUNS,),
            )
        # Tenant-referencing observability rows (the sweep emits an
        # approval_timed_out pipeline_log event). Delete before the tenant.
        for tbl in ("pipeline_log", "pipeline_steps"):
            try:
                conn.execute(
                    f"DELETE FROM {tbl} WHERE tenant_id = ANY(%s::uuid[])",
                    (SEEDED_TENANTS,),
                )
            except psycopg.Error:
                pass  # best-effort; table may not reference tenant_id
        conn.execute(
            "DELETE FROM tenants WHERE id = ANY(%s::uuid[])", (SEEDED_TENANTS,)
        )


def run_canary() -> int:
    _preflight()
    dsn = _dsn()

    from langgraph.types import Command

    from orchestrator import graph as graphmod

    graphmod.init_substrate(dsn)
    pii_seen = False
    try:
        saver = graphmod.get_checkpointer()
        graph = _build_gate_graph(saver)

        # --- Leg 1: pause -> resume(approved) ---
        tid, rid = _seed_run(dsn)
        cfg = {"configurable": {"thread_id": rid}}
        paused = graph.invoke(_request(tid, rid), config=cfg)
        assertion(1, "pause surfaces __interrupt__", "__interrupt__" in paused,
                  observed=list(paused.keys()))

        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT decision, status FROM pending_approvals WHERE run_id = %s",
                (rid,),
            ).fetchone()
        assertion(2, "pending_approvals row, decision NULL",
                  row is not None and row[0] is None and row[1] == "pending",
                  observed=row)

        resumed = graph.invoke(Command(resume={"decision": "approved"}), config=cfg)
        assertion(3, "resume(approved) -> owner_decision='approved'",
                  resumed.get("owner_decision") == "approved",
                  observed=resumed.get("owner_decision"))

        with psycopg.connect(dsn, autocommit=True) as conn:
            n = conn.execute(
                "SELECT count(*) FROM pending_approvals WHERE run_id = %s", (rid,)
            ).fetchone()[0]
        assertion(4, "idempotency: exactly ONE approval row after resume",
                  n == 1, observed=n, expected=1)

        # --- Leg 2: Pillar-7 cannot-bypass — an UNRESOLVED run never approves ---
        tid2, rid2 = _seed_run(dsn)
        cfg2 = {"configurable": {"thread_id": rid2}}
        paused2 = graph.invoke(_request(tid2, rid2), config=cfg2)
        # Still paused; no resume issued. The state has no approved decision.
        bypassed = paused2.get("owner_decision") == "approved"
        assertion(5, "Pillar-7: unresolved run does NOT yield approved",
                  (not bypassed) and ("__interrupt__" in paused2),
                  observed={"owner_decision": paused2.get("owner_decision")})

        # --- Leg 3: timeout sweep ---
        tid3, rid3 = _seed_run(dsn)
        # Seed an OPEN approval already past its timeout_at.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET ROLE app_role")
            conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tid3,))
            conn.execute(
                "INSERT INTO pending_approvals "
                "(tenant_id, run_id, approval_type, summary, timeout_at) "
                "VALUES (%s, %s, 'campaign_send', 'sweep me', %s)",
                (tid3, rid3, datetime.now(UTC) - timedelta(hours=1)),
            )
            conn.execute("SELECT set_config('app.current_tenant', '', false)")
            conn.execute("RESET ROLE")
        # Pre-pause the run so resume(timeout) has a checkpoint to resume.
        cfg3 = {"configurable": {"thread_id": rid3}}
        # The sweep's resume re-enters the gate node; arm is a no-op (an open
        # row exists). We must first create the checkpoint by invoking once.
        graph.invoke(_request(tid3, rid3), config=cfg3)

        from orchestrator.scheduled_triggers import run_approval_timeout_sweep_body

        swept = run_approval_timeout_sweep_body(now=datetime.now(UTC))
        with psycopg.connect(dsn, autocommit=True) as conn:
            d, s, resolved = conn.execute(
                "SELECT decision, status, resolved_at FROM pending_approvals "
                "WHERE run_id = %s",
                (rid3,),
            ).fetchone()
        assertion(6, "timeout sweep -> decision='timeout', status='timed_out', resolved",
                  d == "timeout" and s == "timed_out" and resolved is not None,
                  observed={"decision": d, "status": s, "resolved": resolved is not None,
                            "swept_count": len(swept)})

        # --- A7: CL-390 no PII anywhere persisted in the approval rows ---
        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT summary, details::text, owner_message_sid FROM pending_approvals "
                "WHERE tenant_id = ANY(%s::uuid[])",
                (SEEDED_TENANTS,),
            ).fetchall()
        for summary, details, sid in rows:
            blob = f"{summary} {details} {sid}".lower()
            if "+9198" in blob or "phone" in blob:
                pii_seen = True
        assertion(7, "CL-390: no phone / PII in persisted approval rows",
                  not pii_seen, observed={"rows_checked": len(rows)})

    finally:
        _cleanup(dsn)
        graphmod.reset_substrate()

    failed = [n for n, r in RESULTS.items() if r["status"] == "FAIL"]
    print()
    if failed:
        print(f"CANARY FAIL — assertions failed: {sorted(failed)}", file=sys.stderr)
        return 1
    print(f"CANARY PASS — {len(RESULTS)}/7 assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
