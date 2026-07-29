"""VT-721 S1 — the week-plan store + deterministic revision gate.

Unit: the gate truth table (cap, shape, enums, dup keys, §0.1.1 sticky approval, notes).
Realdb (DATABASE_URL): chain round-trip, per-day idempotence, RLS isolation, purge membership.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402

from orchestrator.business_plan import week_plan as wp  # noqa: E402


def _action(**over):
    base = {
        "key": "a1", "objective": "recover lapsed customers", "directive": "draft win-back",
        "assigned_to": "sales_recovery", "inputs": {"cohort": "lapsed_45d"},
    }
    base.update(over)
    return base


# --- gate truth table ----------------------------------------------------------------------


def test_gate_normalizes_minimal_action():
    acts, notes = wp.gate_revision([_action()], [])
    assert acts[0]["status"] == "planned" and acts[0]["source"] == "reactive"
    assert acts[0]["requires_approval"] is False


def test_gate_cap_rejects_not_truncates():
    with pytest.raises(wp.RevisionRejected):
        wp.gate_revision([_action(key=f"a{i}") for i in range(wp.MAX_ACTIONS + 1)], [])


def test_gate_rejects_missing_triple():
    for missing in ("objective", "directive", "assigned_to", "key"):
        with pytest.raises(wp.RevisionRejected):
            wp.gate_revision([_action(**{missing: ""})], [])


def test_gate_rejects_duplicate_keys():
    with pytest.raises(wp.RevisionRejected):
        wp.gate_revision([_action(), _action()], [])


def test_gate_rejects_unknown_enums():
    with pytest.raises(wp.RevisionRejected):
        wp.gate_revision([_action(status="someday")], [])
    with pytest.raises(wp.RevisionRejected):
        wp.gate_revision([_action(source="dream")], [])


def test_gate_stamps_approval_on_effect_classes():
    """§0.1.1: every effect-class action requires approval, whatever the proposal said."""
    for cls in sorted(wp.EFFECT_ACTION_CLASSES):
        acts, _ = wp.gate_revision([_action(action_class=cls, requires_approval=False)], [])
        assert acts[0]["requires_approval"] is True, cls


def test_gate_approval_is_sticky_true():
    """The gate may ADD the requirement but never clear one the proposal carried."""
    acts, _ = wp.gate_revision([_action(action_class=None, requires_approval=True)], [])
    assert acts[0]["requires_approval"] is True


def test_gate_notes_need_known_kind_and_reason():
    with pytest.raises(wp.RevisionRejected):
        wp.gate_revision([_action()], [{"change": "vibes", "reason": "x"}])
    with pytest.raises(wp.RevisionRejected):
        wp.gate_revision([_action()], [{"change": "drop", "reason": ""}])
    _, notes = wp.gate_revision([_action()], [{"action_key": "a1", "change": "keep", "reason": "on track"}])
    assert notes[0]["change"] == "keep"


def test_write_revision_gated_rejection_returns_none(monkeypatch):
    called = []
    monkeypatch.setattr("orchestrator.db.tenant_connection", lambda t: called.append(t))
    out = wp.write_revision(uuid4(), [_action(status="someday")], [])
    assert out is None and called == []  # rejected BEFORE any DB touch


def test_purge_order_membership():
    from orchestrator.dsr_purge import _PURGE_ORDER

    assert "tenant_week_plans" in _PURGE_ORDER


# --- realdb layer --------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-721 substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
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


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, owner_phone) "
            "VALUES ('VT-721 week-plan test', 'founding', 'paid_active', %s, %s) RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
        return row[0]


@pytestmark_db
def test_chain_roundtrip_and_daily_idempotence(substrate):
    t = _new_tenant(substrate.dsn)
    d1, d2 = date.today() - timedelta(days=1), date.today()
    p1 = wp.write_revision(t, [_action()], [], plan_date=d1)
    assert p1 is not None
    assert wp.write_revision(t, [_action(key="dup")], [], plan_date=d1) is None  # same-day: no-op
    p2 = wp.write_revision(
        t, [_action(key="a2", objective="follow up quotes", directive="chase 3 open quotes")],
        [{"action_key": "a1", "change": "drop", "reason": "cohort empty after outcomes read"}],
        plan_date=d2,
    )
    assert p2 is not None
    latest = wp.latest_plan(t)
    assert latest is not None and latest.plan_date == d2
    assert latest.prev_plan_id == p1  # the chain
    assert latest.revision_notes[0]["change"] == "drop"
    assert latest.horizon_end == d2 + timedelta(days=6)


@pytestmark_db
def test_cross_tenant_isolation(substrate):
    a, b = _new_tenant(substrate.dsn), _new_tenant(substrate.dsn)
    wp.write_revision(a, [_action()], [])
    assert wp.latest_plan(b) is None
