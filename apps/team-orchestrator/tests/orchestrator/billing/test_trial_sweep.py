"""VT-90 / VT-365 — trial sweep canary (Rule #15, real PG, zero-LLM).

Drives run_trial_evaluation_body over seeded trial states and asserts the
decision→action mapping:
  - expire → apply_transition('trial_expired') (→ lapsed) + a trial_subscribe_link
             nudge fired ONCE.
  - warn   → trial_ending notify, no transition.
  - none   → nothing.
apply_transition (a DBOS.step, unreliable outside a workflow) is patched to a
recorder, so this asserts the sweep's INTENT — the real phase mutation is
apply_transition's own (tested in transitions). Idempotent + cross-tenant.

VT-365 (Fazal 2026-06-09): 30-day flat trial, NO card, opt-in subscribe at day 30,
NO auto-charge, no extensions, no refund/clawback. The old extend/exhaust +
trial_max_reached paths are GONE; a trial WARNs then EXPIRES to `lapsed`.
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

# VT-365 flat trial length (config/trial.yaml trial_days: 30, warn_lead_days: 2).
_TRIAL_DAYS = 30
_WARN_LEAD = 2


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


def _tenant(pool, *, phase="trial", trial_start=_T0, paid=None) -> str:
    tid = str(uuid.uuid4())
    with pool.connection() as c:
        c.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, trial_started_at, "
            "paid_conversion_at) VALUES (%s,'tc','founding',%s,%s,%s)",
            (tid, phase, trial_start, paid),
        )
    return tid


@pytest.fixture
def patched(monkeypatch):
    """Patch apply_transition → recorder; stub the subscribe-link compose to a
    deterministic dict (so the expire→notify path doesn't depend on OWNER_JWT_SECRET /
    a real token mint). Return (transitions, notifies) lists."""
    transitions: list[tuple] = []
    notifies: list[tuple] = []

    def _fake_apply(state, event, context):
        transitions.append((str(state["tenant_id"]), event))
        return state

    monkeypatch.setattr("orchestrator.transitions.apply_transition", _fake_apply)
    monkeypatch.setattr(
        "orchestrator.billing.trial_sweep._compose_trial_subscribe_link",
        lambda tid: {"owner_name": "there", "subscribe_link": "https://viabe.ai/team/subscribe?t=x"},
    )
    return transitions, notifies


def test_sweep_expire_transitions_and_nudges(pool, patched):
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    transitions, notifies = patched
    # a: at/after the 30-day end with no subscribe → expire (→ lapsed) + subscribe-link nudge.
    a = _tenant(pool, trial_start=_T0)
    # d: mid-trial → none.
    d = _tenant(pool, trial_start=_T0 + timedelta(days=200))

    now = _T0 + timedelta(days=_TRIAL_DAYS + 5)  # a past end → expire; d mid-trial → none.
    run_trial_evaluation_body(
        now=now, notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl)),
    )

    tmap = dict(transitions)
    assert tmap.get(a) == "trial_expired"  # VT-365: expire → lapsed, NOT extend/exhaust
    assert d not in tmap  # mid-trial → no action
    ntpl = {t: tpl for t, tpl in notifies}
    assert ntpl.get(a) == "trial_subscribe_link"  # the one-shot trial-end conversion nudge
    # No card/auto-charge/refund event is ever fired by the sweep.
    assert all(ev == "trial_expired" for _t, ev in transitions)


def test_sweep_warn_no_transition(pool, patched):
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    transitions, notifies = patched
    w = _tenant(pool, trial_start=_T0)  # end T0+30; warn at T0+28
    run_trial_evaluation_body(
        now=_T0 + timedelta(days=_TRIAL_DAYS - _WARN_LEAD, hours=2),
        notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl)),
    )
    assert (w, "trial_ending") in [(t, tpl) for t, tpl in notifies]
    assert w not in dict(transitions)  # warn does not transition


def test_sweep_expire_is_idempotent_after_lapse(pool, patched):
    from orchestrator.billing.trial_sweep import run_trial_evaluation_body

    transitions, notifies = patched
    # A tenant already moved to lapsed is out of the trial scan (phase='trial' only) →
    # the sweep never re-expires it.
    m = _tenant(pool, phase="lapsed", trial_start=_T0)
    run_trial_evaluation_body(
        now=_T0 + timedelta(days=_TRIAL_DAYS + 10),
        notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl)),
    )
    assert m not in dict(transitions)
    assert m not in {t for t, _ in notifies}


def test_sweep_passes_per_tenant_language_into_notify(pool, patched, monkeypatch):
    """VT-426 (Row D): the sweep resolves the tenant's preferred language and passes
    THAT variant into the notify call — not a hardcoded 'en'. A 'hi'-preference tenant
    on the warn day → the notify receives ('trial_ending', 'hi')."""
    from orchestrator.billing import trial_sweep as ts

    transitions, notifies = patched
    monkeypatch.setattr(ts, "_preferred_language", lambda tid: "hi")

    w = _tenant(pool, trial_start=_T0)  # end T0+30; warn at T0+28
    ts.run_trial_evaluation_body(
        now=_T0 + timedelta(days=_TRIAL_DAYS - _WARN_LEAD, hours=2),
        notify_fn=lambda t, tpl, lang, p: notifies.append((str(t), tpl, lang)),
    )
    assert (w, "trial_ending", "hi") in notifies
