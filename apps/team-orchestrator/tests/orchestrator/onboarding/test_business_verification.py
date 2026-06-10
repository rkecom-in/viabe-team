"""VT-361 — two-tier business verification. Real-PG canary (synthetic; CL-422).

Proves: GSTIN-lookup → gstin_verified; fail-closed unverified with vendor_down vs invalid_gstin
distinguished; per-day cap; VTR "green" override + audit; the ACTIVATION GATE both directions
(below-threshold blocked, at-threshold passes, server-side read); DSR anonymize fold-in. The
real-Sandbox GSTIN call is a gated post-creds step (fail-not-skip once creds land).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
import psycopg  # noqa: E402

from orchestrator.integrations.methods.sandbox_kyc import GstinLookup  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

_ACTIVE = lambda g: GstinLookup(ok=True, legal_name="RKECOM SERVICE (OPC) PRIVATE LIMITED", status="Active")  # noqa: E731


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _tenant(dsn: str, *, name: str = "Acme Traders", phase: str = "trial") -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, owner_phone) "
            "VALUES (%s, 'founding', %s, %s, %s) RETURNING id",
            (name, phase, f"+9199{uuid4().int % 10**8:08d}", f"+9188{uuid4().int % 10**8:08d}"),
        ).fetchone()[0])


def _row(dsn, tid) -> dict:
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(
            "SELECT verification_status, verified_business_name, gstin, verification_method, phase "
            "FROM tenants WHERE id = %s", (tid,)
        ).fetchone()


# --- tiers -------------------------------------------------------------------
def test_active_gstin_earns_gstin_verified(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    out = run_lookup(tid, "27AAKCR3738B1ZE", search_fn=_ACTIVE)
    assert out["ok"] and out["status"] == "gstin_verified"
    r = _row(substrate, tid)
    assert r["verification_status"] == "gstin_verified"
    assert r["verified_business_name"] == "RKECOM SERVICE (OPC) PRIVATE LIMITED"
    assert r["gstin"] == "27AAKCR3738B1ZE" and r["verification_method"] == "gstin_lookup"


def test_vendor_down_is_retryable_unverified(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    out = run_lookup(tid, "27AAKCR3738B1ZE", search_fn=lambda g: GstinLookup(ok=False))
    assert not out["ok"] and out["reason"] == "vendor_down"
    assert _row(substrate, tid)["verification_status"] == "unverified"
    with psycopg.connect(substrate, autocommit=True) as c:
        outcome = c.execute(
            "SELECT outcome FROM kyc_verification_log WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 1",
            (tid,)).fetchone()[0]
    assert outcome == "vendor_down"  # distinct from invalid_gstin (ops: outage, not bad input)


def test_inactive_gstin_is_invalid_not_outage(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    out = run_lookup(tid, "27AAKCR3738B1ZE", search_fn=lambda g: GstinLookup(
        ok=True, legal_name="X", status="Cancelled"))
    assert not out["ok"] and out["reason"] == "invalid_gstin"
    assert _row(substrate, tid)["verification_status"] == "unverified"


def test_attempt_cap(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    for _ in range(5):
        run_lookup(tid, "27AAKCR3738B1ZE", search_fn=_ACTIVE)
    assert run_lookup(tid, "27AAKCR3738B1ZE", search_fn=_ACTIVE) == {"ok": False, "reason": "attempt_cap"}


# --- VTR "green" override ----------------------------------------------------
def test_vtr_override_sets_green_and_audits(substrate):
    from orchestrator.onboarding.verification import run_vtr_override

    tid = _tenant(substrate)
    op = str(uuid4())
    out = run_vtr_override(tid, op, "manual review — verified via call")
    assert out["ok"] and out["status"] == "vtr_verified"
    assert _row(substrate, tid)["verification_status"] == "vtr_verified"
    with psycopg.connect(substrate, autocommit=True) as c:
        n = c.execute("SELECT count(*) FROM ops_audit WHERE operator_id=%s AND action='vtr_verify'", (op,)).fetchone()[0]
    assert n == 1  # audit trail (load-bearing when green gains significance)


# --- the ACTIVATION GATE (both directions, server-side) ----------------------
def test_gate_blocks_activation_below_gstin_verified(substrate):
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import VerificationRequiredError, apply_transition

    tid = _tenant(substrate)  # unverified
    state = new_subscriber_state(tenant_id=UUID(tid), run_id=uuid4(), phase="trial")
    with pytest.raises(VerificationRequiredError):
        apply_transition(state, "subscribe", {"reason": "test"})  # VT-365: gate on subscribe
    assert _row(substrate, tid)["phase"] == "trial"  # NOT activated


def test_gate_allows_activation_when_gstin_verified(substrate):
    from orchestrator.onboarding.verification import run_lookup
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition

    tid = _tenant(substrate)
    run_lookup(tid, "27AAKCR3738B1ZE", search_fn=_ACTIVE)  # → gstin_verified
    state = new_subscriber_state(tenant_id=UUID(tid), run_id=uuid4(), phase="trial")
    apply_transition(state, "subscribe", {"reason": "test"})  # VT-365: gate on subscribe
    assert _row(substrate, tid)["phase"] == "paid_active"


def test_gate_allows_activation_when_vtr_verified(substrate):
    from orchestrator.onboarding.verification import run_vtr_override
    from orchestrator.state import new_subscriber_state
    from orchestrator.transitions import apply_transition

    tid = _tenant(substrate)
    run_vtr_override(tid, str(uuid4()), "green")  # vtr_verified is above the gstin floor
    state = new_subscriber_state(tenant_id=UUID(tid), run_id=uuid4(), phase="trial")
    apply_transition(state, "subscribe", {"reason": "test"})  # VT-365: gate on subscribe
    assert _row(substrate, tid)["phase"] == "paid_active"


# --- DSR fold-in -------------------------------------------------------------
def test_dsr_purge_scrubs_gstin_and_verified_name(substrate):
    from orchestrator.dsr_purge import purge_tenant_data
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate, phase="paid_active")
    run_lookup(tid, "27AAKCR3738B1ZE", search_fn=_ACTIVE)
    with psycopg.connect(substrate, autocommit=True) as conn:
        ticket = str(conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id", (tid,)
        ).fetchone()[0])
    purge_tenant_data(UUID(ticket))
    r = _row(substrate, tid)
    assert r["gstin"] is None and r["verified_business_name"] is None
    with psycopg.connect(substrate, autocommit=True) as c:
        n = c.execute("SELECT count(*) FROM kyc_verification_log WHERE tenant_id=%s", (tid,)).fetchone()[0]
    assert n == 0


# --- gated real-Sandbox canary (Rule #15; fail-not-skip once creds land) ------
@pytest.mark.skipif(not os.environ.get("SANDBOX_API_KEY"), reason="SANDBOX_API_KEY not set — post-creds canary")
def test_real_gstin_lookup_fazal():
    from orchestrator.integrations.methods.sandbox_kyc import search_gstin

    res = search_gstin("27AAKCR3738B1ZE")  # Fazal's, consented
    assert res.ok, "real Sandbox GSTIN lookup must succeed once creds land"
    assert "RKECOM" in (res.authoritative_name() or "").upper()
