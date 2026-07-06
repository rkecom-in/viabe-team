"""VT-610 (Package 7, item 5) — VERIFY + test ``takeover.py``, no source changes.

Recon confirmed takeover is already atomic (composing the two EXISTING enforcement primitives —
``workflow_controls`` pause + ``autonomy.vtr_autonomy_override('freeze')``, which atomically cancels
in-flight batches): this row needed no build here, only proof. Covers:

  - ``take_over_tenant`` pauses ``agent_dispatch`` + freezes EVERY registered agent, cancelling any
    open batch atomically (the SAME binding kill-switch rule every freeze honors).
  - Idempotent re-takeover: the pause is ON CONFLICT DO NOTHING; re-freezing an already-frozen
    agent is a clean no-op.
  - ``release_takeover`` unfreezes every agent and releases the pause — NEVER promotes. Proven
    against BOTH an agent that was at the L2 default AND one that was force_l3'd (VT-610) before
    the takeover froze it: release only ever flips ``frozen`` back to False; ``level`` is
    untouched either way (a forced-L3 agent survives a takeover/release cycle still forced-L3,
    a plain L2 agent stays L2 — "release never promotes" holds for both starting states).
  - Idempotent release.

DB substrate mirrors ``tests/orchestrator/agents/test_autonomy.py``, EXCEPT the connection kind
for ``take_over_tenant``/``release_takeover`` themselves: ``workflow_controls`` (migration 131) is
RLS+FORCE with ZERO policies (deny-all under FORCE — only the privileged service pool ever reaches
it), so — matching the REAL caller, ``ops_run_control.py``'s ``/takeover``/``/release-takeover``
endpoints — these two calls run on ``get_pool().connection()``, never ``tenant_connection`` (which
would 403/InsufficientPrivilege on the workflow_controls INSERT). ``force_l3`` (writing
``tenant_agent_autonomy``, which DOES have real per-tenant RLS policies) still uses
``tenant_connection`` as usual.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # tenant_connection -> orchestrator.graph pulls langgraph

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.agents.autonomy import force_l3, get_autonomy  # noqa: E402
from orchestrator.agents.takeover import (  # noqa: E402
    _registered_agents,
    release_takeover,
    take_over_tenant,
)
from orchestrator.db import tenant_connection  # noqa: E402
from orchestrator.graph import get_pool  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-610 takeover tests skipped",
)

pytestmark = requires_db

AGENT = "sales_recovery"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) --------------------


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('VT-610 takeover test', 'founding', 'trial', now(), 'restaurant', %s) "
            "RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, *, agent: str = AGENT, status: str = "awaiting_approval") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        wi = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, %s, 'drafting') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", agent),
        ).fetchone()
        assert wi is not None
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(wi[0]), agent, status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _batch_status(dsn: str, tenant: UUID, batch: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _pause_row(dsn: str, tenant: UUID) -> dict[str, object] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT set_by, released_at FROM workflow_controls "
            "WHERE tenant_id = %s AND workflow_kind = 'agent_dispatch' "
            "ORDER BY set_at DESC LIMIT 1",
            (str(tenant),),
        ).fetchone()
    if row is None:
        return None
    return {"set_by": row[0], "released_at": row[1]}


# ---------------------------------------------------------------------------
# take_over_tenant: pauses dispatch + freezes every agent + cancels open work atomically
# ---------------------------------------------------------------------------


def test_take_over_tenant_pauses_and_freezes_every_agent(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch = _seed_batch(dsn, tenant)
    operator = str(uuid4())

    with get_pool().connection() as conn:
        result = take_over_tenant(tenant, operator_id=operator, reason="fraud suspected", conn=conn)

    assert result["paused"] is True
    assert sorted(result["frozen_agents"]) == _registered_agents()  # ALL registered agents
    pause = _pause_row(dsn, tenant)
    assert pause is not None
    assert pause["released_at"] is None
    for agent in _registered_agents():
        assert get_autonomy(tenant, agent).frozen is True
    # The binding atomic-cancel rule: a kill switch never leaves armed batches ticking.
    assert _batch_status(dsn, tenant, batch) == "cancelled"


def test_take_over_tenant_is_idempotent(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    operator = str(uuid4())

    with get_pool().connection() as conn:
        take_over_tenant(tenant, operator_id=operator, reason="first", conn=conn)
    with get_pool().connection() as conn:
        result = take_over_tenant(tenant, operator_id=operator, reason="again", conn=conn)

    assert result["paused"] is True
    for agent in _registered_agents():
        assert get_autonomy(tenant, agent).frozen is True
    # ON CONFLICT DO NOTHING: still exactly one active (unreleased) pause row.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM workflow_controls WHERE tenant_id = %s "
            "AND workflow_kind = 'agent_dispatch' AND released_at IS NULL",
            (str(tenant),),
        ).fetchone()
    assert row is not None
    assert row[0] == 1


# ---------------------------------------------------------------------------
# release_takeover: unfreezes every agent + releases the pause — NEVER promotes
# ---------------------------------------------------------------------------


def test_release_takeover_unfreezes_and_never_promotes_l2_agent(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    operator = str(uuid4())

    with get_pool().connection() as conn:
        take_over_tenant(tenant, operator_id=operator, reason="x", conn=conn)
    with get_pool().connection() as conn:
        result = release_takeover(tenant, operator_id=operator, reason="resolved", conn=conn)

    assert result["released"] is True
    assert sorted(result["unfrozen_agents"]) == _registered_agents()
    pause = _pause_row(dsn, tenant)
    assert pause is not None
    assert pause["released_at"] is not None
    for agent in _registered_agents():
        st = get_autonomy(tenant, agent)
        assert st.frozen is False
        assert st.level == "L2"  # NEVER promoted — the agent was L2 before, L2 after


def test_release_takeover_unfreezes_but_never_touches_level_of_a_forced_l3_agent(substrate) -> None:
    """The sharper 'release never promotes' proof: an agent that was FORCE_L3'd (VT-610) before
    the takeover froze it survives the takeover/release cycle STILL forced-L3 — release only ever
    flips ``frozen``, it neither strips a forced grant nor (the actual "never promotes" claim)
    grants L3 to any OTHER agent that never had it."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    operator = str(uuid4())
    forced_agent = AGENT
    other_agents = [a for a in _registered_agents() if a != forced_agent]
    assert other_agents  # sanity: there IS at least one other registered agent to prove didn't-promote on

    with tenant_connection(tenant) as conn:
        force_l3(tenant, forced_agent, vtr_id=operator, reason="pre-takeover force", conn=conn)
    with get_pool().connection() as conn:
        take_over_tenant(tenant, operator_id=operator, reason="x", conn=conn)
    assert get_autonomy(tenant, forced_agent).level == "L3"  # still L3 while frozen (freeze != revoke)
    assert get_autonomy(tenant, forced_agent).frozen is True

    with get_pool().connection() as conn:
        release_takeover(tenant, operator_id=operator, reason="resolved", conn=conn)

    forced_state = get_autonomy(tenant, forced_agent)
    assert forced_state.frozen is False
    assert forced_state.level == "L3"  # the forced grant survives — unfreeze never strips level
    for agent in other_agents:
        other_state = get_autonomy(tenant, agent)
        assert other_state.frozen is False
        assert other_state.level == "L2"  # release never promotes an UNRELATED agent either


def test_release_takeover_is_idempotent(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    operator = str(uuid4())

    with get_pool().connection() as conn:
        take_over_tenant(tenant, operator_id=operator, reason="x", conn=conn)
    with get_pool().connection() as conn:
        release_takeover(tenant, operator_id=operator, reason="first", conn=conn)
    with get_pool().connection() as conn:
        result = release_takeover(tenant, operator_id=operator, reason="again", conn=conn)

    assert result["released"] is True
    for agent in _registered_agents():
        assert get_autonomy(tenant, agent).frozen is False
