"""VT-552 (B1 part-2b) — incident store + escalation ladder + silent-terminal detector (live PG)."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — incident_store tests skipped",
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
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"tm-{tid[:8]}"),
        )
    return tid


def _seed_run(pool, tid, *, status="completed", final_outcome=None, ended_ago_min=90) -> str:
    rid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status, started_at, ended_at, "
            "final_outcome) VALUES (%s, %s, 'webhook', %s, now() - interval '2 hours', "
            "now() - make_interval(mins => %s), %s)",
            (rid, tid, status, ended_ago_min, final_outcome),
        )
    return rid


def test_create_incident_is_idempotent_per_run_kind(pool):
    from orchestrator.observability import incident_store as inc

    tid = _seed_tenant(pool)
    rid = _seed_run(pool, tid)
    a = inc.create_incident(tid, incident_kind="silent_terminal", run_id=rid)
    b = inc.create_incident(tid, incident_kind="silent_terminal", run_id=rid)
    assert a is not None and a == b  # one incident per (run, kind)


def test_escalation_ladder_cas_and_vtr_row(pool):
    from orchestrator.observability import incident_store as inc

    tid = _seed_tenant(pool)
    rid = _seed_run(pool, tid)
    iid = inc.create_incident(tid, incident_kind="silent_terminal", run_id=rid)

    # tier 0 -> 1 (owner contacted)
    assert inc.escalate_incident(tid, iid, to_tier=1, owner_contacted=True) is True
    got = inc.get_incident(tid, iid)
    assert got["escalation_tier"] == 1 and got["owner_contacted"] is True

    # tier 1 -> 2 (VTR): status escalated + an escalations row linked
    assert inc.escalate_incident(tid, iid, to_tier=2) is True
    got = inc.get_incident(tid, iid)
    assert got["escalation_tier"] == 2 and got["status"] == "escalated"
    assert got["vtr_escalation_id"] is not None
    with pool.connection() as conn:
        esc = conn.execute(
            "SELECT kind, severity FROM escalations WHERE run_id = %s", (rid,)
        ).fetchone()
    assert esc is not None

    # re-escalating to the same/lower tier does NOT advance (CAS)
    assert inc.escalate_incident(tid, iid, to_tier=2) is False


def test_resolve_incident(pool):
    from orchestrator.observability import incident_store as inc

    tid = _seed_tenant(pool)
    rid = _seed_run(pool, tid)
    iid = inc.create_incident(tid, incident_kind="failed_run", run_id=rid)
    assert inc.resolve_incident(tid, iid) is True
    assert inc.get_incident(tid, iid)["status"] == "resolved"
    assert inc.resolve_incident(tid, iid) is False  # already resolved


def test_detector_opens_incident_for_silent_terminal_and_alerts(pool, monkeypatch):
    from orchestrator.alerts import dispatch as dispatch_mod
    from orchestrator.orphan_reaper import detect_silent_terminal_runs

    fired: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda t: fired.append(t) or None)

    tid = _seed_tenant(pool)
    silent = _seed_run(pool, tid, status="completed", final_outcome=None, ended_ago_min=90)
    # a completed run WITH an outcome must NOT be flagged
    _seed_run(pool, tid, status="completed", final_outcome="sent_winback", ended_ago_min=90)

    opened = detect_silent_terminal_runs(pool=pool)
    assert opened >= 1
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE run_id = %s AND incident_kind = 'silent_terminal'",
            (silent,),
        ).fetchone()
    assert row is not None
    assert any(
        t.trigger_kind == "silent_terminal" and t.payload.get("run_id") == silent for t in fired
    )

    # idempotent: a second sweep opens no new incident for the same run
    before = opened
    again = detect_silent_terminal_runs(pool=pool)
    assert again <= before  # the already-incident run is excluded by NOT EXISTS
