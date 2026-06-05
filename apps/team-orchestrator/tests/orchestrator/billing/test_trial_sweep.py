"""VT-90 — trial sweep canary (Rule #15, real PG, zero-LLM).

Drives run_trial_evaluation_body over seeded trial states and asserts the
decision→action mapping: extend → trial_extension_granted + offer notify; exhaust
→ trial_extension_exhausted (+ max_reached notify only when cap-driven); warn →
trial_ending notify, no transition; none → nothing. apply_transition (a DBOS.step,
unreliable outside a workflow) is patched to a recorder, so this asserts the sweep's
INTENT — the real phase mutation is apply_transition's own (tested in transitions).
Idempotent + cross-tenant.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-90 trial sweep canary skipped",
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
            "INSERT INTO tenants (id, business_name, plan_tier, phase, trial_started_at, "
            "trial_extension_count, paid_conversion_at) VALUES (%s,'tc','founding',%s,%s,%s,%s)",
            (tid, phase, trial_start, count, paid),
        )
    return tid


def _campaign(pool, tid, *, status="sent", generated_at=_T0):
    with pool.connection() as c:
        run = c.execute("INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s,'running') "
                        "RETURNING id", (tid,)).fetchone()["id"]
        c.execute("INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at) "
                  "VALUES (%s,%s,'{}'::jsonb,%s,%s)", (tid, run, status, generated_at))


@pytest.fixture
def patched(monkeypatch):
    """Patch apply_transition → recorder; return (transitions, notifies) lists."""
    transitions: list[tuple] = []
    notifies: list[tuple] = []

    def _fake_apply(state, event, context):
        transitions.append((str(state["tenant_id"]), event))
        return state

    monkeypatch.setattr("orchestrator.transitions.apply_transition", _fake_apply)
    return transitions, notifies


def test_sweep_decision_to_action(pool, patched):
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    transitions, notifies = patched
    a = _tenant(pool, trial_start=_T0, count=0)          # engaged, at end → extend
    _campaign(pool, a, generated_at=_T0 + timedelta(days=3))
    b = _tenant(pool, trial_start=_T0)                    # not engaged, grace over → exhaust
    d = _tenant(pool, trial_start=_T0 + timedelta(days=60))  # mid-trial → none

    now = _T0 + timedelta(days=22)  # a: end+8 engaged → extend; b: end+8 → exhaust.
    run_trial_evaluation_body(
        now=now, notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl)),
    )

    tmap = dict(transitions)
    assert tmap.get(a) == "trial_extension_granted"
    assert tmap.get(b) == "trial_extension_exhausted"
    assert d not in tmap  # mid-trial → no action
    ntpl = {t: tpl for t, tpl in notifies}
    assert ntpl.get(a) == "trial_extension_offered"
    # b exhausted by non-engagement (not cap) → cancelled, NO trial_max_reached notify.
    assert b not in ntpl


def test_sweep_warn_no_transition(pool, patched):
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    transitions, notifies = patched
    w = _tenant(pool, trial_start=_T0)  # end T0+14; warn at T0+12
    run_trial_evaluation_body(
        now=_T0 + timedelta(days=12, hours=2),
        notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl)),
    )
    assert (w, "trial_ending") in [(t, tpl) for t, tpl in notifies]
    assert w not in dict(transitions)  # warn does not transition


def test_sweep_cap_exhaust_notifies_max_reached(pool, patched):
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    transitions, notifies = patched
    # count=3 (cap), engaged, end+grace passed → exhaust + trial_max_reached notify.
    m = _tenant(pool, phase="trial_extended", trial_start=_T0, count=3)
    _campaign(pool, m, generated_at=_T0 + timedelta(days=40))
    run_trial_evaluation_body(
        now=_T0 + timedelta(days=56 + 8),
        notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl)),
    )
    assert dict(transitions).get(m) == "trial_extension_exhausted"
    assert (m, "trial_max_reached") in [(t, tpl) for t, tpl in notifies]
