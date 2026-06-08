"""VT-3.2 tests — phase transitions, invariants, tenants mirror, auto-resume.

Require a live Postgres via ``DATABASE_URL`` plus the dbos / langgraph stack;
run in the CI ``orchestrator`` job.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import psycopg  # noqa: E402 — imported after the dependency skip guards

from orchestrator.transitions import TRANSITIONS  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — transition tests skipped",
)

_WORKER = Path(__file__).parent / "_transition_resume_worker.py"
_VALID_TRANSITIONS = [(f, e, t) for (f, e), t in TRANSITIONS.items()]


@pytest.fixture(scope="module")
def tx():
    """Apply migrations, launch DBOS, expose transitions + invariants."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos
    from orchestrator import invariants, transitions
    from orchestrator.state import new_subscriber_state

    launch_dbos()
    try:
        yield SimpleNamespace(
            dsn=dsn,
            transitions=transitions,
            invariants=invariants,
            make_state=new_subscriber_state,
        )
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str, phase: str = "onboarding") -> str:
    # VT-361: transition-mechanics tests assume an activatable tenant, so seed gstin_verified —
    # the card_captured → paid_active activation gate (transitions.py) is exercised separately in
    # tests/orchestrator/onboarding/test_business_verification.py (both directions).
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "verification_status) "
            "VALUES ('VT-3.2 Test', 'founding', %s, now(), 'gstin_verified') RETURNING id",
            (phase,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _state(tx, tenant_id, phase, **overrides):
    """A SubscriberState for transition tests.

    paid_conversion_at is pre-set so transitions that legitimately involve a
    paid phase satisfy the paid-conversion invariant.
    """
    state = tx.make_state(UUID(str(tenant_id)), phase=phase)
    state["paid_conversion_at"] = datetime(2026, 1, 1, tzinfo=UTC)
    state.update(overrides)
    return state


def _wait_for_count(dsn: str, sql: str, params: tuple, target: int, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with psycopg.connect(dsn, autocommit=True) as conn:
            count = conn.execute(sql, params).fetchone()[0]
        if count >= target:
            return
        time.sleep(0.5)
    raise AssertionError(f"condition not met within {timeout}s: {sql}")


# --- Valid + invalid transitions --------------------------------------------


@pytest.mark.parametrize("from_phase,event,to_phase", _VALID_TRANSITIONS)
def test_valid_transition_fires(tx, from_phase, event, to_phase):
    tenant_id = _new_tenant(tx.dsn, phase=from_phase)
    state = _state(tx, tenant_id, from_phase)
    new_state = tx.transitions.apply_transition(state, event, {})

    assert new_state["phase"] == to_phase
    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT from_phase, to_phase, event FROM phase_transitions "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    assert row == (from_phase, to_phase, event)


@pytest.mark.parametrize(
    "from_phase,event",
    [
        ("onboarding", "card_captured"),
        ("refunded", "signup"),
        ("cancelled", "manual_cancel"),
        ("trial", "engagement_recovered"),
        ("paid_active", "signup"),
    ],
)
def test_invalid_transition_raises(tx, from_phase, event):
    tenant_id = _new_tenant(tx.dsn, phase=from_phase)
    state = _state(tx, tenant_id, from_phase)
    with pytest.raises(tx.transitions.InvalidTransitionError):
        tx.transitions.apply_transition(state, event, {})


def test_trial_extension_cap_blocks_fourth_grant(tx):
    tenant_id = _new_tenant(tx.dsn, phase="trial_extended")
    state = _state(tx, tenant_id, "trial_extended", trial_extension_count=3)
    with pytest.raises(tx.transitions.InvalidTransitionError):
        tx.transitions.apply_transition(state, "trial_extension_granted", {})


def test_side_effect_fields_updated(tx):
    tenant_id = _new_tenant(tx.dsn, phase="onboarding")
    state = _state(tx, tenant_id, "onboarding")
    state["paid_conversion_at"] = None
    state["trial_started_at"] = None

    after_signup = tx.transitions.apply_transition(state, "signup", {})
    assert after_signup["trial_started_at"] is not None

    after_card = tx.transitions.apply_transition(after_signup, "card_captured", {})
    assert after_card["paid_conversion_at"] is not None
    assert after_card["phase"] == "paid_active"


# --- Invariants (rollback) ---------------------------------------------------


def test_invariant_paid_without_conversion_rolls_back(tx):
    tenant_id = _new_tenant(tx.dsn, phase="paid_active")
    state = _state(tx, tenant_id, "paid_active")
    state["paid_conversion_at"] = None  # corrupt: paid phase, no conversion

    with pytest.raises(tx.invariants.InvariantViolationError):
        tx.transitions.apply_transition(state, "weekly_low_engagement", {})

    # Rolled back: no transition row written, tenants.phase unchanged.
    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM phase_transitions WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]
        phase = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]
    assert rows == 0
    assert phase == "paid_active"


def test_invariant_trial_extension_cap_check(tx):
    state = tx.make_state(uuid4(), phase="trial_extended")
    state["trial_extension_count"] = 4
    with psycopg.connect(tx.dsn) as conn, pytest.raises(
        tx.invariants.InvariantViolationError
    ):
        tx.invariants.check_invariants(state, conn, uuid4())


def test_invariant_monotonic_rolls_back(tx):
    tenant_id = _new_tenant(tx.dsn, phase="onboarding")
    future = datetime.now(UTC) + timedelta(days=1)
    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO phase_transitions (tenant_id, to_phase, event, transition_at) "
            "VALUES (%s, 'trial', 'signup', %s)",
            (tenant_id, future),
        )

    state = _state(tx, tenant_id, "onboarding")
    with pytest.raises(tx.invariants.InvariantViolationError):
        tx.transitions.apply_transition(state, "signup", {})

    # Rolled back: only the seeded future row remains.
    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM phase_transitions WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]
    assert rows == 1


# --- Mirror ------------------------------------------------------------------


def test_tenants_phase_mirror_reflects_latest(tx):
    tenant_id = _new_tenant(tx.dsn, phase="onboarding")
    state = _state(tx, tenant_id, "onboarding")
    state["paid_conversion_at"] = None

    state = tx.transitions.apply_transition(state, "signup", {})
    state = tx.transitions.apply_transition(state, "card_captured", {})

    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        phase = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]
    assert phase == "paid_active"
    assert state["phase"] == "paid_active"


# --- DBOS auto-resume --------------------------------------------------------


def test_dbos_auto_resumes_mid_transition(tx):
    """A workflow SIGKILLed after a transition step resumes; the transition is
    applied exactly once (DBOS caches the completed step)."""
    dsn = tx.dsn
    tenant_id = _new_tenant(dsn, phase="onboarding")
    workflow_id = f"tx-resume-{uuid4()}"

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _resume_probe ("
            "id serial PRIMARY KEY, workflow_id text, step_label text, "
            "at timestamptz DEFAULT now())"
        )

    proc1 = subprocess.Popen([sys.executable, str(_WORKER), dsn, workflow_id, tenant_id])
    try:
        _wait_for_count(
            dsn,
            "SELECT count(*) FROM phase_transitions WHERE tenant_id = %s",
            (tenant_id,),
            1,
            timeout=50,
        )
    finally:
        proc1.kill()
    proc1.wait(timeout=15)

    proc2 = subprocess.Popen([sys.executable, str(_WORKER), dsn, workflow_id, tenant_id])
    try:
        _wait_for_count(
            dsn,
            "SELECT count(*) FROM _resume_probe WHERE workflow_id = %s",
            (workflow_id,),
            1,
            timeout=90,
        )
    finally:
        proc2.kill()
        proc2.wait(timeout=15)

    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM phase_transitions WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()[0]
        phase = conn.execute(
            "SELECT phase FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]
    assert rows == 1, f"signup transition applied {rows}x — must be exactly once"
    assert phase == "trial"


# --- VT-333 — founding-slot release on cancellation (audit-only) ----------------------------
def test_cancelled_transition_releases_founding_slot_audit_only(tx):
    """VT-333: a cancelled transition stamps founding_tier_claims.released_at (audit-only). The
    counter's claimed_count is NEVER decremented (no-reopen policy → zero integrity risk)."""
    from orchestrator.billing.founding_counter import try_claim_founding_slot
    from orchestrator.graph import get_pool

    tenant_id = _new_tenant(tx.dsn, phase="trial")
    with get_pool().connection() as c:
        try_claim_founding_slot(c, tenant_id)  # service-role claim
    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        before = conn.execute(
            "SELECT claimed_count FROM founding_tier_counter WHERE id = 1"
        ).fetchone()[0]
        claim = conn.execute(
            "SELECT released_at FROM founding_tier_claims WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
    assert claim is not None and claim[0] is None  # claimed, not yet released

    new_state = tx.transitions.apply_transition(_state(tx, tenant_id, "trial"), "manual_cancel", {})
    assert new_state["phase"] == "cancelled"

    with psycopg.connect(tx.dsn, autocommit=True) as conn:
        released = conn.execute(
            "SELECT released_at FROM founding_tier_claims WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]
        after = conn.execute(
            "SELECT claimed_count FROM founding_tier_counter WHERE id = 1"
        ).fetchone()[0]
    assert released is not None, "cancelled transition must stamp released_at (audit)"
    assert after == before, "claimed_count must be UNCHANGED (no-reopen; no decrement)"
