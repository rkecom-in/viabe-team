"""VT-558 (B6) — run-control completeness: campaign true-kill CAS + VTR takeover (live Postgres).

Proves the two net-new controls (LANE freeze already exists as per-agent autonomy freeze):
  * CampaignsWrapper.cancel — CAS a non-terminal campaign → 'cancelled' (killable only from
    proposed/approved; a sent/cancelled campaign is a no-op).
  * take_over_tenant / release_takeover — seize a tenant: pause agent_dispatch + freeze EVERY
    registered agent (atomic in-flight cancel), then reverse it. Composed over existing primitives.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-558 tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'paid_active')",
            (tid, f"vt558-{tid[:8]}"),
        )
    return tid


def _seed_campaign(pool, tid: str, *, status: str = "proposed") -> str:
    with pool.connection() as conn:
        run_id = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (tid,),
        ).fetchone()["id"]
        cid = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json) "
            "VALUES (%s, %s, %s, now(), '{}'::jsonb) RETURNING id",
            (tid, run_id, status),
        ).fetchone()["id"]
    return str(cid)


# --- campaign true-kill CAS -----------------------------------------------------------------------


def test_wrapper_cancel_kills_non_terminal_campaign(pool):
    from orchestrator.db.wrappers import CampaignsWrapper

    tid = _seed_tenant(pool)
    cid = _seed_campaign(pool, tid, status="proposed")
    assert CampaignsWrapper().cancel(tid, cid) is True  # proposed → cancelled
    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM campaigns WHERE id = %s", (cid,)).fetchone()
    assert row["status"] == "cancelled"


def test_wrapper_cancel_is_noop_on_terminal(pool):
    from orchestrator.db.wrappers import CampaignsWrapper

    tid = _seed_tenant(pool)
    cid = _seed_campaign(pool, tid, status="sent")
    assert CampaignsWrapper().cancel(tid, cid) is False  # sent is terminal — not killable
    # a second kill of an already-cancelled campaign is also a no-op
    cid2 = _seed_campaign(pool, tid, status="proposed")
    assert CampaignsWrapper().cancel(tid, cid2) is True
    assert CampaignsWrapper().cancel(tid, cid2) is False


# --- VTR takeover ---------------------------------------------------------------------------------


def _registered_agents():
    from orchestrator.business_plan.store import OWNING_AGENTS

    return sorted(OWNING_AGENTS - {"unassigned"})


def test_takeover_pauses_dispatch_and_freezes_all_agents(pool):
    from orchestrator.agents.takeover import release_takeover, take_over_tenant

    tid = _seed_tenant(pool)
    op = str(uuid4())
    agents = _registered_agents()

    with pool.connection() as conn:
        result = take_over_tenant(tid, operator_id=op, reason="test takeover", conn=conn)
    assert result["paused"] is True
    assert sorted(result["frozen_agents"]) == agents

    with pool.connection() as conn:
        hold = conn.execute(
            "SELECT count(*) AS n FROM workflow_controls "
            "WHERE tenant_id = %s AND workflow_kind = 'agent_dispatch' AND released_at IS NULL",
            (tid,),
        ).fetchone()["n"]
        frozen = conn.execute(
            "SELECT count(*) AS n FROM tenant_agent_autonomy WHERE tenant_id = %s AND frozen",
            (tid,),
        ).fetchone()["n"]
    assert hold == 1  # agent_dispatch paused → coordinator skips this tenant
    assert frozen == len(agents)  # every lane frozen (in-flight batches cancelled)

    with pool.connection() as conn:
        rel = release_takeover(tid, operator_id=op, reason="done", conn=conn)
    assert rel["released"] is True

    with pool.connection() as conn:
        hold_after = conn.execute(
            "SELECT count(*) AS n FROM workflow_controls "
            "WHERE tenant_id = %s AND workflow_kind = 'agent_dispatch' AND released_at IS NULL",
            (tid,),
        ).fetchone()["n"]
        frozen_after = conn.execute(
            "SELECT count(*) AS n FROM tenant_agent_autonomy WHERE tenant_id = %s AND frozen",
            (tid,),
        ).fetchone()["n"]
    assert hold_after == 0  # hold released
    assert frozen_after == 0  # all unfrozen


def test_takeover_is_idempotent(pool):
    from orchestrator.agents.takeover import take_over_tenant

    tid = _seed_tenant(pool)
    op = str(uuid4())
    with pool.connection() as conn:
        take_over_tenant(tid, operator_id=op, reason="first", conn=conn)
    with pool.connection() as conn:
        take_over_tenant(tid, operator_id=op, reason="second", conn=conn)  # no duplicate hold
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM workflow_controls "
            "WHERE tenant_id = %s AND workflow_kind = 'agent_dispatch' AND released_at IS NULL",
            (tid,),
        ).fetchone()["n"]
    assert n == 1  # ON CONFLICT kept a single active hold
