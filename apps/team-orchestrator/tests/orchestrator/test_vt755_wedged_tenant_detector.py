"""VT-755 scope 3 — a tenant whose Manager is WEDGED must page a human.

WHAT "WEDGED" MEANS, and why it is critical rather than another stall warning. A task parked
``waiting_owner`` with no open approval and no ``wait_workflow_id`` stamp has no escape:

  * ``_wake_waiting_workflow`` fires only from ``mark_approval_resolved`` — there is no approval;
  * it also needs the wake stamp, which is NULL on these rows;
  * ``reap_stalled_manager_tasks`` deliberately EXCLUDES ``waiting_owner`` (``task_store.py:280``) so it
    can never burn an awaiting-approval task to dead_letter — correct for approvals, fatal here;
  * ``correlate_reply`` only flips a row to ``answered`` and sends no DBOS wake.

And because ``waiting_owner`` is in ``TASK_ACTIVE``, ``promote_next_queued_task`` refuses to advance
anything behind it — while the promoter is only ever called from a TERMINAL task's tail, which this task
never reaches. **Every later objective for that tenant queues behind it forever.**

Measured on deployed dev 2026-08-14: 4 of 7 ``waiting_owner`` tasks were in this state, 1 tenant already
had a ``queued`` task stranded behind one.

Per the exit gate, the detector is proven by FORCING the state, not by reading it. The negative cases
matter as much as the positive one: a detector that also fires on the legitimate 48h approval park would
be turned off within a week.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("langgraph")  # orphan_reaper -> graph

import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-755 wedge-detector tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _tenant(conn) -> str:
    return str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('vt755 wedge', 'founding', 'trial') RETURNING id"
        ).fetchone()[0]
    )


def _task(conn, tenant_id, *, status, stall=None, age_hours=5, objective="wedge probe") -> str:
    """A task whose updated_at is backdated past the detector's grace window."""
    tid = str(
        conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective, status, attempt, max_attempts, "
            " stall_metadata) VALUES (%s, %s::jsonb, %s, 0, 3, %s) RETURNING id",
            (tenant_id, f'{{"goal": "{objective}"}}', status, Jsonb(stall) if stall else None),
        ).fetchone()[0]
    )
    conn.execute(
        "UPDATE manager_tasks SET updated_at = now() - make_interval(hours => %s) WHERE id = %s",
        (age_hours, tid),
    )
    return tid


def _open_approval(conn, tenant_id) -> None:
    """An OPEN approval, with the real `pipeline_runs` row its FK requires.

    The run row is not decoration: `pending_approvals.run_id` is FK-constrained, so a synthetic uuid4
    would make this fixture fail to insert and the negative test would then pass for the wrong reason —
    "no alert fired" because no approval existed, rather than because the detector correctly ignored a
    wakeable park.
    """
    run_id = str(
        conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'manager_dispatch', 'paused') RETURNING id",
            (tenant_id,),
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO pending_approvals (tenant_id, approval_type, status, run_id, summary) "
        "VALUES (%s, 'campaign_send', 'pending', %s, 'vt755 legitimate approval park')",
        (tenant_id, run_id),
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture dispatched Triggers instead of sending them."""
    from orchestrator.alerts import dispatch as dispatch_mod

    seen: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda trig: seen.append(trig))
    import orchestrator.orphan_reaper as reaper

    monkeypatch.setattr(reaper, "_alert_wedged_tenants", reaper._alert_wedged_tenants)
    return seen


@pytest.mark.integration
def test_an_unwakeable_park_is_detected_and_alerted(dsn, _dbpool, monkeypatch):
    """THE DEFECT, forced. No approval, no wake stamp, past the grace window."""
    import orchestrator.orphan_reaper as reaper

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = _tenant(conn)
        task_id = _task(conn, tenant_id, status="waiting_owner", stall=None)
        _task(conn, tenant_id, status="queued", objective="stranded behind the wedge")

    seen: list = []
    monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", lambda t: seen.append(t))

    found = reaper.detect_wedged_tenants(pool=_dbpool)

    assert found >= 1, "an un-wakeable park past the grace window was not detected"
    mine = [t for t in seen if t.payload.get("task_id") == task_id]
    assert len(mine) == 1, f"expected exactly one alert for the wedged task, got {len(mine)}"
    trig = mine[0]
    assert trig.trigger_kind == "wedged_tenant"
    assert trig.severity == "critical", (
        "every other stall kind is a warning; this one ENDS the tenant's Manager and nothing recovers "
        "unaided, so it must page"
    )
    assert trig.payload["queued_behind"] == 1, (
        "the alert must carry how many objectives are stranded — that number is the difference between "
        "'one stuck job' and 'this tenant is dead'"
    )
    assert "queued behind it" in trig.message_text and "cannot run" in trig.message_text.lower(), (
        "the message must name the CONSEQUENCE; an operator reading 'task parked' triages it as one job"
    )


@pytest.mark.integration
def test_the_LEGITIMATE_approval_park_is_NOT_flagged(dsn, _dbpool, monkeypatch):
    """The negative case that decides whether this detector survives contact with operators. An
    approval park is wakeable by design and runs to a 48h TTL — flagging it would make the alert noise
    and it would be muted, taking the real signal with it."""
    import orchestrator.orphan_reaper as reaper

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = _tenant(conn)
        task_id = _task(conn, tenant_id, status="waiting_owner", stall=None)
        _open_approval(conn, tenant_id)

    seen: list = []
    monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", lambda t: seen.append(t))
    reaper.detect_wedged_tenants(pool=_dbpool)

    assert not [t for t in seen if t.payload.get("task_id") == task_id], (
        "a park with an OPEN approval is wakeable via mark_approval_resolved -> _wake_waiting_workflow "
        "and must never be reported as wedged"
    )


@pytest.mark.integration
def test_a_park_with_a_WAKE_STAMP_is_NOT_flagged(dsn, _dbpool, monkeypatch):
    """The other escape route. VT-671 stamps the live workflow id so the resolution seam can DBOS.send
    to it; a stamped park is reachable even with no approval row visible to this query."""
    import orchestrator.orphan_reaper as reaper

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = _tenant(conn)
        task_id = _task(
            conn, tenant_id, status="waiting_owner",
            stall={"wait_workflow_id": "manager_task:t:1-redrive-2"},
        )

    seen: list = []
    monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", lambda t: seen.append(t))
    reaper.detect_wedged_tenants(pool=_dbpool)

    assert not [t for t in seen if t.payload.get("task_id") == task_id], (
        "a park carrying a wait_workflow_id is wakeable and must not be flagged"
    )


@pytest.mark.integration
def test_a_FRESH_park_is_not_flagged_yet(dsn, _dbpool, monkeypatch):
    """Grace window. A park whose approval/stamp write is still in flight must not be called wedged —
    the whole point of the 2h default is that a race is not a wedge."""
    import orchestrator.orphan_reaper as reaper

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = _tenant(conn)
        task_id = _task(conn, tenant_id, status="waiting_owner", stall=None, age_hours=0)

    seen: list = []
    monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", lambda t: seen.append(t))
    reaper.detect_wedged_tenants(pool=_dbpool)

    assert not [t for t in seen if t.payload.get("task_id") == task_id]


@pytest.mark.integration
def test_the_detector_mutates_nothing(dsn, _dbpool, monkeypatch):
    """It detects and alerts; it does not un-wedge. Un-wedging means deciding the parked objective's
    fate (cancel? escalate? ask something answerable?) which is a product call, not a sweep's. A sweep
    that quietly cancelled owner-facing work would be a far worse defect than the one it fixes."""
    import orchestrator.orphan_reaper as reaper

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = _tenant(conn)
        task_id = _task(conn, tenant_id, status="waiting_owner", stall=None)
        qid = _task(conn, tenant_id, status="queued", objective="stays queued")

    monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", lambda t: None)
    reaper.detect_wedged_tenants(pool=_dbpool)

    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM manager_tasks WHERE id = %s", (task_id,)
        ).fetchone()[0] == "waiting_owner", "the detector moved the parked task"
        assert conn.execute(
            "SELECT status FROM manager_tasks WHERE id = %s", (qid,)
        ).fetchone()[0] == "queued", "the detector promoted a queued task"


@pytest.mark.integration
def test_the_new_kind_is_writable_by_the_database(dsn):
    """VT-746's class, applied to this row's own addition: a kind declared in Python that the CHECK
    refuses is an alert that can never fire. Migration 205 exists because of that test, and this is the
    direct assertion for the kind added here."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = _tenant(conn)
        conn.execute(
            "INSERT INTO tenant_alerts (tenant_id, trigger_kind, severity, dedup_key, message_text) "
            "VALUES (%s, 'wedged_tenant', 'critical', %s, 'vt755 writability probe')",
            (tenant_id, f"vt755:{uuid4()}"),
        )
        assert conn.execute(
            "SELECT count(*) FROM tenant_alerts WHERE tenant_id = %s "
            "AND trigger_kind = 'wedged_tenant'",
            (tenant_id,),
        ).fetchone()[0] == 1
