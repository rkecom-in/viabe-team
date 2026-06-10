"""VT-90 / VT-365 — trial-evaluator decision canary (Rule #15, real PG, zero-LLM).

Deterministic: seed a tenant's trial state, drive `now`, assert the decision
(warn / expire / none). No LLM.

VT-365 (Fazal 2026-06-09): 30-day flat trial, NO card in trial, owner opt-in
`subscribe` at/after day 30, NO auto-charge, no extensions, no refund. The trial
WARNs (at trial_end - warn_lead) then EXPIRES to `lapsed`. The old
engagement-gated extend/exhaust + grace paths are GONE — so are the `engaged` /
`extension_count` verdict fields and campaign-engagement seeding.
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
            "INSERT INTO tenants (id, business_name, plan_tier, phase, "
            "trial_started_at, paid_conversion_at) "
            "VALUES (%s, 'trial-co', 'founding', %s, %s, %s)",
            (tid, phase, trial_start, paid),
        )
    return tid


def test_mid_trial_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=5))
    assert v.decision == "none"


def test_warn_lead_is_warn(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # trial_end = T0+30; warn at T0+(30-2) = T0+28.
    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=_TRIAL_DAYS - _WARN_LEAD, hours=1))
    assert v.decision == "warn"


def test_just_before_warn_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # one day before the warn lead → still nothing due.
    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=_TRIAL_DAYS - _WARN_LEAD - 1))
    assert v.decision == "none"


def test_at_trial_end_expires(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # VT-365: at/after the flat 30-day end with no subscribe → expire (→ lapsed). No
    # engagement check, no grace, no extension.
    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=_TRIAL_DAYS, hours=1))
    assert v.decision == "expire"


def test_past_trial_end_expires(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # Well past the end (where the old model would have exhausted) → still just expire.
    t = _tenant(pool, trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=_TRIAL_DAYS + 30))
    assert v.decision == "expire"


def test_subscribed_tenant_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # paid_conversion_at set → owner has subscribed → out of scope.
    t = _tenant(
        pool, phase="paid_active", trial_start=_T0, paid=_T0 + timedelta(days=10)
    )
    v = evaluate_trial(t, now=_T0 + timedelta(days=_TRIAL_DAYS + 5))
    assert v.decision == "none"


def test_lapsed_tenant_is_none(pool):
    from orchestrator.billing.trial_evaluator import evaluate_trial

    # An already-expired (lapsed) tenant is out of scope — the sweep only acts on phase='trial'.
    t = _tenant(pool, phase="lapsed", trial_start=_T0)
    v = evaluate_trial(t, now=_T0 + timedelta(days=_TRIAL_DAYS + 5))
    assert v.decision == "none"
