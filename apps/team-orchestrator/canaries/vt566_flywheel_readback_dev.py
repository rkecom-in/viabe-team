"""VT-565/VT-566 flywheel loop-closure PROOF on deployed dev (run via `railway run` so the dev DB
URL + MANAGER_MEMORY_RETRIEVAL inject OS-env→process — never into CC's context).

Proves, against the LIVE dev Supabase:
  1. VT-565 B2 producer — an objective-bearing dispatch mints a durable manager_task (planned→
     running) and the run terminal settles it (completed + done step): the spine has real rows.
  2. VT-561/566 capture→read-back — record_correction (a reject with the owner's prose + proposal
     snapshot, first-party authority) is retrieval-eligible AT CAPTURE and get_recent_lessons
     returns it.
  3. Decision C tier branch — an EXPLICIT (emoji) owner_feedback row renders as authoritative; an
     IMPLICIT row renders ONLY in the down-weighted `## Outcome signals (weak)` block.
  4. The NEXT-RUN context block renders (MANAGER_MEMORY_RETRIEVAL ON on dev): the captured reject
     lesson from "run N" is present in the manager context built for "run N+1" — the flywheel turns.

Creates + DELETES a clearly-bogus test tenant (NOT real customer data — CL-422 ok). Prints only
PASS/FAIL + non-secret evidence (ids, booleans, rendered block text — content is PII-redacted at
capture by construction). No DB URL / secret echoed.
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

_TID = str(uuid4())
_RUN_N = str(uuid4())
_RUN_N1 = str(uuid4())
_AGENT = "sales_recovery"


def _init_pool() -> None:
    dsn = os.environ.get("TEAM_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("FAIL: no TEAM_SUPABASE_DB_URL / DATABASE_URL in env (run via `railway run`)")
        sys.exit(2)
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=2,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )


def main() -> int:
    _init_pool()
    from orchestrator import graph as graph_mod

    pool = graph_mod.get_pool()
    ok = True

    with pool.connection() as c:
        c.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'paid_active')",
            (_TID, f"vt566-devprobe-{_TID[:8]}"),
        )
        for rid in (_RUN_N, _RUN_N1):
            c.execute(
                "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
                "VALUES (%s, %s, 'orchestrator', 'running')",
                (rid, _TID),
            )
    print(f"[setup] bogus test tenant {_TID} + 2 runs created")

    try:
        # 1. VT-565 B2 producer on live dev.
        from orchestrator.manager import task_store
        from orchestrator.manager.task_producer import on_route_decided, on_run_completed

        on_route_decided(
            {"tenant_id": _TID, "run_id": _RUN_N, "trigger_reason": "dev_probe"}, _AGENT
        )
        on_run_completed(_TID, _RUN_N)
        task_id = task_store.find_task_id(_TID, f"live_dispatch:{_RUN_N}")
        task = task_store.get_task(_TID, task_id) if task_id else None
        steps = task_store.get_steps(_TID, task_id) if task_id else []
        b2_ok = (
            task is not None and task["status"] == "completed"
            and len(steps) == 1 and steps[0]["status"] == "done"
        )
        print(f"[step1] B2 producer: task_minted={task_id is not None} "
              f"status={task['status'] if task else None} steps_done={b2_ok}")
        ok = ok and b2_ok

        # 2. Capture a reject lesson ("run N") — first-party, eligible at capture.
        from orchestrator.agents.correction_store import get_recent_lessons, record_correction

        with pool.connection() as c:
            record_correction(
                c, _TID, agent=_AGENT, correction_kind="reject", decision_verb="rejected",
                owner_feedback="Too pushy — soften the tone and drop the discount.",
                run_id=_RUN_N,
                proposal_snapshot={"drafts": [{"template_name": "team_winback_simple",
                                               "params": {"days_since_last_visit": "45"}}],
                                   "draft_count": 1, "captured": 1, "truncated": False},
            )
        lessons = get_recent_lessons(_TID, agent=_AGENT)
        hit = next((le for le in lessons if le.get("kind") == "reject"), None)
        cap_ok = hit is not None and "soften" in (hit.get("correction_text") or "")
        print(f"[step2] capture→read-back: eligible_at_capture+returned={cap_ok} "
              f"(n_lessons={len(lessons)})")
        ok = ok and cap_ok

        # 3. Tier branch: one explicit (emoji) + one implicit outcome row.
        with pool.connection() as c:
            c.execute(
                "INSERT INTO owner_feedback (tenant_id, run_id, tier, signal) VALUES "
                "(%s, %s, 'emoji', 'thumbs_up'), (%s, %s, 'implicit', 'thumbs_down')",
                (_TID, _RUN_N, _TID, _RUN_N),
            )

        # 4. The NEXT-RUN ("run N+1") manager context block renders with all three, correctly tiered.
        from uuid import UUID

        from orchestrator.agent.dispatch import (
            _build_manager_lessons_block,
            _manager_memory_retrieval_enabled,
        )

        flag = _manager_memory_retrieval_enabled()
        block = _build_manager_lessons_block(UUID(_TID))
        render_ok = (
            flag and block is not None
            and "## Lessons from this owner" in block
            and "soften the tone" in block
            and "thumbs_up" in block
            and "## Outcome signals (weak)" in block
            and "[weak signal — outcome-derived, not owner-stated] thumbs_down" in block
            # the implicit thumbs_down must NOT appear in the authoritative section:
            and block.index("thumbs_down") > block.index("## Outcome signals (weak)")
        )
        print(f"[step3/4] MANAGER_MEMORY_RETRIEVAL={flag} next_run_block_renders={render_ok}")
        if block:
            print("----- rendered block (next-run manager context) -----")
            print(block)
            print("-----------------------------------------------------")
        ok = ok and render_ok

    finally:
        with pool.connection() as c:
            # FK order: tm_audit_log + pipeline_runs/steps do NOT cascade on tenant delete.
            for tbl in ("tm_audit_log", "pipeline_steps", "pipeline_runs"):
                c.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (_TID,))  # noqa: S608 — fixed list
            c.execute("DELETE FROM tenants WHERE id = %s", (_TID,))  # cascades the rest
            left = c.execute(
                "SELECT count(*) AS n FROM tenants WHERE id = %s", (_TID,)
            ).fetchone()
        pool.close()  # stop worker threads cleanly (avoids the shutdown warnings)
        print(f"[cleanup] test tenant deleted (rows left={left['n'] if isinstance(left, dict) else left[0]})")

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} — B2 spine + capture→next-run read-back "
          "(Decision-C tiering) proven on live dev.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
