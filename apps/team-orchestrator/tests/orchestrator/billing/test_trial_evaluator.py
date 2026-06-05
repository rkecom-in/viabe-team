"""VT-90 — trial-evaluator decision canary (Rule #15, real PG, zero-LLM).

Deterministic: seed a tenant's trial state + campaigns, drive `now`, assert the
decision (warn / extend / exhaust / none). No LLM.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-90 trial evaluator canary skipped",
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield graph_mod._pool
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev


def _tenant(pool, *, phase="trial", trial_start=_T0, count=0, paid=None) -> str:
    tid = str(uuid.uuid4())
    with pool.connection() as c:
        c.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, "
            "trial_started_at, trial_extension_count, paid_conversion_at) "
            "VALUES (%s, 'trial-co', 'founding', %s, %s, %s, %s)",
            (tid, phase, trial_start, count, paid),
        )
    return tid


def _campaign(pool, tid, *, status="approved", generated_at=_T0) -> None:
    with pool.connection() as c:
        run = c.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') "
            "RETURNING id", (tid,),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at) "
            "VALUES (%s, %s, '{}'::jsonb, %s, %s)",
            (tid, run, status, generated_at),
        )


def test_mid_trial_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=5))
    assert v.decision == "none"


def test_day12_is_warn(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, trial_start=_T0)  # trial_end = T0+14; warn at T0+12
    v = evaluate_trial(t, now=_T0 + timedelta(days=12, hours=1))
    assert v.decision == "warn"


def test_trial_end_engaged_extends(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, trial_start=_T0, count=0)
    _campaign(pool, t, status="sent", generated_at=_T0 + timedelta(days=3))
    v = evaluate_trial(t, now=_T0 + timedelta(days=14, hours=1))
    assert v.decision == "extend"
    assert v.engaged is True


def test_trial_end_not_engaged_in_grace_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, trial_start=_T0)  # no campaigns → not engaged
    v = evaluate_trial(t, now=_T0 + timedelta(days=15))  # past end, within grace (7d)
    assert v.decision == "none"
    assert v.engaged is False


def test_grace_expired_exhausts(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=22))  # end+8 > grace(7)
    assert v.decision == "exhaust"


def test_at_extension_cap_exhausts(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # count=3 (cap); trial_end = T0 + 14*4 = T0+56. Engaged but no room → grace → exhaust.
    t = _tenant(pool, phase="trial_extended", trial_start=_T0, count=3)
    _campaign(pool, t, status="sent", generated_at=_T0 + timedelta(days=40))
    v = evaluate_trial(t, now=_T0 + timedelta(days=56 + 8))  # past end + grace
    assert v.decision == "exhaust"


def test_paid_tenant_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, phase="paid_active", trial_start=_T0, paid=_T0 + timedelta(days=10))
    v = evaluate_trial(t, now=_T0 + timedelta(days=20))
    assert v.decision == "none"
