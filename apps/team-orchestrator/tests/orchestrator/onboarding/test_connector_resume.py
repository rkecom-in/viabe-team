"""VT-608 fix round, CRITICAL 1 — the connector-routing dispatcher
(``onboarding.connector_resume``) + the Sheets-specific resume hook
(``onboarding.sheets_resume``).

THE DEFECT: ``tenant_integration_state`` has ONE row per tenant. Before this fix, ``runner.py``
called the Shopify-only ``maybe_resume_shopify_onboarding`` unconditionally for ANY tenant with a
live auth-phase pending — a Sheets-flow tenant either dead-ended (no Shopify token exists) or, if
a stale Shopify token happened to be on file, could fire a Shopify ORDER INGEST off a Sheets "done"
reply. This suite proves the dispatcher routes correctly and that a Sheets flow NEVER invokes the
Shopify ingest path (spied), end to end against a real Postgres.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-608 connector-resume tests skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt608-resume-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _seed_tenant(dsn: str) -> UUID:
    tid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, owner_phone) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (str(tid), f"vt608-resume-{str(tid)[:8]}", f"+9198{uuid4().int % 10**8:08d}"),
        )
    return tid


def _seed_state(dsn: str, tenant_id: UUID, *, phase: str, connector_id: str | None, awaiting: str) -> None:
    import json

    pending = {
        "awaiting": awaiting,
        "prompt_text": "x",
        "connector_id": connector_id,
        "metadata": {},
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase, current_connector_id, "
            "pending_owner_input) VALUES (%s, %s, %s, %s::jsonb) "
            "ON CONFLICT (tenant_id) DO UPDATE SET phase = EXCLUDED.phase, "
            "current_connector_id = EXCLUDED.current_connector_id, "
            "pending_owner_input = EXCLUDED.pending_owner_input",
            (str(tenant_id), phase, connector_id, json.dumps(pending)),
        )


def _seed_sheets_token(dsn: str, tenant_id: UUID) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_oauth_tokens (tenant_id, connector_id, refresh_token_encrypted, scopes) "
            "VALUES (%s, 'google_sheet', 'enc-placeholder', '{}')",
            (str(tenant_id),),
        )


def _read_state(dsn: str, tenant_id: UUID) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT phase, pending_owner_input FROM tenant_integration_state WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    return {"phase": row[0], "pending_owner_input": row[1]}


def test_no_state_row_returns_none(substrate):
    from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

    tid = _seed_tenant(substrate.dsn)
    assert maybe_resume_connector_onboarding(tid, "done", "sid-1", "+911234567890") is None


def test_shopify_connector_routes_to_shopify_hook_unchanged(substrate, monkeypatch):
    """The dispatcher's shopify/None branch is byte-identical: it calls the EXACT existing
    shopify hook, untouched."""
    import orchestrator.onboarding.connector_resume as connector_resume_mod

    tid = _seed_tenant(substrate.dsn)
    _seed_state(substrate.dsn, tid, phase="phase_2_auth", connector_id="shopify", awaiting="oauth_completion")

    called = {}

    def _spy(tenant_id, body, message_sid, recipient):
        called["tenant_id"] = tenant_id
        return {"done": False, "phase": "phase_2_auth", "routed": "spy_shopify"}

    monkeypatch.setattr(
        "orchestrator.onboarding.shopify_onboarding.maybe_resume_shopify_onboarding", _spy
    )
    result = connector_resume_mod.maybe_resume_connector_onboarding(tid, "done", "sid-2", "+911234567890")
    assert result == {"done": False, "phase": "phase_2_auth", "routed": "spy_shopify"}
    assert called["tenant_id"] == tid


def test_sheets_flow_not_connected_never_invokes_shopify_ingest(substrate, monkeypatch):
    """THE adversarial proof: a Sheets tenant replying 'done' must NEVER reach
    pull_and_ingest_shopify — the exact cross-wire the CRITICAL 1 finding described."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

    tid = _seed_tenant(substrate.dsn)
    _seed_state(
        substrate.dsn, tid, phase="phase_2_auth", connector_id="google_sheet", awaiting="oauth_completion"
    )

    def _forbidden_ingest(tenant_id, **kwargs):
        raise AssertionError("pull_and_ingest_shopify must NEVER be invoked for a Sheets tenant")

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _forbidden_ingest)

    result = maybe_resume_connector_onboarding(tid, "done", "sid-3", "+911234567890")
    assert result == {"done": False, "phase": "phase_2_auth", "routed": "sheets_auth_not_connected"}
    # state must NOT have advanced — no fabricated progress
    assert _read_state(substrate.dsn, tid)["phase"] == "phase_2_auth"


def test_sheets_data_action_while_oauth_pending_is_honest_no_fabrication(substrate):
    """DF1(a) mint-armed: 'import my orders now' while OAuth is still pending (not connected) must be
    answered HONESTLY (no fabricated import) + stay in phase_2_auth, never advance."""
    from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

    tid = _seed_tenant(substrate.dsn)
    _seed_state(
        substrate.dsn, tid, phase="phase_2_auth", connector_id="google_sheet", awaiting="oauth_completion"
    )
    result = maybe_resume_connector_onboarding(
        tid, "Please go ahead and import my orders now, I mapped Name and Phone", "sid-da", "+911234567890"
    )
    assert result == {"done": False, "phase": "phase_2_auth", "routed": "sheets_data_action_not_connected"}
    assert _read_state(substrate.dsn, tid)["phase"] == "phase_2_auth"  # no fabricated advance


def test_sheets_flow_connected_advances_to_sample_pending(substrate):
    tid = _seed_tenant(substrate.dsn)
    _seed_state(
        substrate.dsn, tid, phase="phase_2_auth", connector_id="google_sheet", awaiting="oauth_completion"
    )
    _seed_sheets_token(substrate.dsn, tid)

    from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

    result = maybe_resume_connector_onboarding(tid, "done", "sid-4", "+911234567890")
    assert result == {"done": False, "phase": "phase_3_sample_pull", "routed": "sheets_oauth_confirmed"}

    state = _read_state(substrate.dsn, tid)
    assert state["phase"] == "phase_3_sample_pull"
    assert state["pending_owner_input"]["awaiting"] == "sample_pull_pending"


def test_sheets_flow_non_done_reply_falls_through(substrate):
    """No Shopify-worded LLM intent classifier reuse — a non-floor reply just falls through."""
    tid = _seed_tenant(substrate.dsn)
    _seed_state(
        substrate.dsn, tid, phase="phase_2_auth", connector_id="google_sheet", awaiting="oauth_completion"
    )

    from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

    result = maybe_resume_connector_onboarding(tid, "what does this cost?", "sid-5", "+911234567890")
    assert result is None
    assert _read_state(substrate.dsn, tid)["phase"] == "phase_2_auth"


def test_unrecognized_connector_id_returns_none_without_crashing(substrate):
    tid = _seed_tenant(substrate.dsn)
    _seed_state(
        substrate.dsn, tid, phase="phase_2_auth", connector_id="amazon_seller_central",
        awaiting="oauth_completion",
    )

    from orchestrator.onboarding.connector_resume import maybe_resume_connector_onboarding

    result = maybe_resume_connector_onboarding(tid, "done", "sid-6", "+911234567890")
    assert result is None
