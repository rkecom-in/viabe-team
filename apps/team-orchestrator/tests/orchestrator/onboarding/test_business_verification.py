"""VT-361 — Option F business verification. Real-PG canary (synthetic; CL-422).

Proves the tiers (gstin_verified ∧ name_verified ∧ fail-closed unverified), the anti-gaming match,
the per-day attempt cap, and the DSR anonymize fold-in. The real-Sandbox GSTIN call is a gated
post-creds acceptance step (fail-not-skip once creds land).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
import psycopg  # noqa: E402

from orchestrator.integrations.methods.sandbox_kyc import GstinLookup, ReversePennyDrop  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


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


def _tenant(dsn: str, *, name: str = "Acme Traders") -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, owner_phone) "
            "VALUES (%s, 'founding', 'onboarding', %s, %s) RETURNING id",
            (name, f"+9199{uuid4().int % 10**8:08d}", f"+9188{uuid4().int % 10**8:08d}"),
        ).fetchone()[0])


def _status(dsn, tid) -> dict:
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(
            "SELECT verification_status, verified_business_name, gstin, verification_method "
            "FROM tenants WHERE id = %s", (tid,)
        ).fetchone()


# --- name_match (pure) -------------------------------------------------------
def test_name_match_handles_legal_suffixes_and_mismatch():
    from orchestrator.onboarding.verification import name_match

    assert name_match("RKECOM SERVICE (OPC) PRIVATE LIMITED", "RKECOM Services")
    assert name_match("Asha Sarees", "ASHA SAREES")
    assert not name_match("Asha Sarees", "Bharat Electronics")
    assert not name_match(None, "anything")


# --- tiers -------------------------------------------------------------------
def test_lookup_records_gstin_but_stays_unverified(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    out = run_lookup(tid, "27AAKCR3738B1ZE", search_fn=lambda g: GstinLookup(
        ok=True, legal_name="RKECOM SERVICE (OPC) PRIVATE LIMITED", status="Active",
        constitution="Proprietorship"))
    assert out["ok"]
    s = _status(substrate, tid)
    assert s["gstin"] == "27AAKCR3738B1ZE"
    assert s["verified_business_name"] == "RKECOM SERVICE (OPC) PRIVATE LIMITED"
    assert s["verification_status"] == "unverified"  # knowledge, not control — bind required


def test_bind_gstin_verified_when_payer_matches_lookup(substrate):
    from orchestrator.onboarding.verification import run_bind, run_lookup

    tid = _tenant(substrate, name="RKECOM")
    run_lookup(tid, "27AAKCR3738B1ZE", search_fn=lambda g: GstinLookup(
        ok=True, legal_name="RKECOM SERVICE (OPC) PRIVATE LIMITED", status="Active"))
    out = run_bind(tid, "ref-1", poll_fn=lambda r: ReversePennyDrop(
        ok=True, reference=r, payer_name="RKECOM Service"))
    assert out["status"] == "gstin_verified"
    s = _status(substrate, tid)
    assert s["verification_status"] == "gstin_verified"
    assert s["verification_method"] == "gstin_reverse_penny_drop"


def test_bind_name_verified_without_gstin(substrate):
    from orchestrator.onboarding.verification import run_bind

    tid = _tenant(substrate, name="Asha Sarees")
    out = run_bind(tid, "ref-2", poll_fn=lambda r: ReversePennyDrop(
        ok=True, reference=r, payer_name="Asha Sarees"))
    assert out["status"] == "name_verified"


def test_bind_mismatch_stays_unverified(substrate):
    from orchestrator.onboarding.verification import run_bind

    tid = _tenant(substrate, name="Asha Sarees")
    out = run_bind(tid, "ref-3", poll_fn=lambda r: ReversePennyDrop(
        ok=True, reference=r, payer_name="Some Unrelated Person"))
    assert not out["ok"] and out["status"] == "unverified"
    assert _status(substrate, tid)["verification_status"] == "unverified"


def test_vendor_down_fails_closed(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    out = run_lookup(tid, "27AAKCR3738B1ZE", search_fn=lambda g: GstinLookup(ok=False))
    assert not out["ok"]
    assert _status(substrate, tid)["verification_status"] == "unverified"


def test_attempt_cap_blocks_storms(substrate):
    from orchestrator.onboarding.verification import run_lookup

    tid = _tenant(substrate)
    ok = lambda g: GstinLookup(ok=True, legal_name="X Co", status="Active")  # noqa: E731
    for _ in range(5):
        run_lookup(tid, "27AAKCR3738B1ZE", search_fn=ok)
    capped = run_lookup(tid, "27AAKCR3738B1ZE", search_fn=ok)
    assert capped == {"ok": False, "reason": "attempt_cap"}


# --- DSR fold-in (correction 3) ---------------------------------------------
def test_dsr_purge_scrubs_gstin_and_verified_name(substrate):
    from orchestrator.dsr_purge import purge_tenant_data
    from orchestrator.onboarding.verification import run_bind, run_lookup

    tid = _tenant(substrate, name="RKECOM")
    run_lookup(tid, "27AAKCR3738B1ZE", search_fn=lambda g: GstinLookup(
        ok=True, legal_name="RKECOM SERVICE (OPC) PRIVATE LIMITED", status="Active"))
    run_bind(tid, "r", poll_fn=lambda r: ReversePennyDrop(ok=True, reference=r, payer_name="RKECOM Service"))
    with psycopg.connect(substrate, autocommit=True) as conn:
        ticket = str(conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id", (tid,)
        ).fetchone()[0])
    purge_tenant_data(UUID(ticket))
    s = _status(substrate, tid)
    assert s["gstin"] is None and s["verified_business_name"] is None  # scrubbed
    with psycopg.connect(substrate, autocommit=True) as conn:
        n = conn.execute("SELECT count(*) FROM kyc_verification_log WHERE tenant_id = %s", (tid,)).fetchone()[0]
    assert n == 0  # verification history swept


# --- gated real-Sandbox canary (Rule #15; fail-not-skip once creds land) -----
@pytest.mark.skipif(not os.environ.get("SANDBOX_API_KEY"), reason="SANDBOX_API_KEY not set — post-creds canary")
def test_real_gstin_lookup_fazal():
    from orchestrator.integrations.methods.sandbox_kyc import search_gstin

    res = search_gstin("27AAKCR3738B1ZE")  # Fazal's, consented for this purpose
    assert res.ok, "real Sandbox GSTIN lookup must succeed once creds land"
    name = (res.legal_name or res.trade_name or "").upper()
    assert "RKECOM" in name, f"expected RKECOM-family name, got {name!r}"
