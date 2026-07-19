"""VT-608 (Loop Package 5) — DB-backed proof for the integration_agent tools that touch
``tenant_integration_state`` / ``tenant_oauth_tokens`` through ``onboarding.shopify_onboarding``'s
module-top-bound ``tenant_connection`` (read_integration_state / check_oauth_status /
confirm_mapping / commit_ingestion / verify_connector). These can't be exercised with a shallow
``orchestrator.db.tenant_connection`` monkeypatch the way ``schedule_recurring_pull``'s own
lazy-import-per-call can (see ``test_integration_agent_tenant_scope.py``'s own note) — a real
Postgres round-trip is both simpler and more valuable here (proves the RLS-scoped write/read
actually lands, not just that a mock was called).

Also covers RULING 3's own invariant directly: ``commit_ingestion`` NEVER writes a ``customers``
row itself — only a ``tenant_integration_state`` PROPOSAL. And RULING 4 (resume): a phase written
by one tool call is read back correctly by a LATER, INDEPENDENT tool call — the same DB-state-only
resume discipline ``shopify_onboarding.py`` already relies on (no in-memory/thread state carried).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-608 integration_agent realdb tests skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt608-test-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _seed_tenant(dsn: str) -> str:
    tid = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt608-{tid[:8]}"),
        )
    return tid


def _seed_oauth_token(dsn: str, tenant_id: str, connector_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_oauth_tokens "
            "(tenant_id, connector_id, refresh_token_encrypted, scopes) "
            "VALUES (%s, %s, 'enc-placeholder', '{}')",
            (tenant_id, connector_id),
        )


def _ctx(run_id, tenant_id):
    from orchestrator.observability.decorators import observability_context

    return observability_context(run_id=run_id, tenant_id=tenant_id)


# --- read_integration_state ---------------------------------------------------------------------


def test_read_integration_state_no_row_returns_none_phase(substrate):
    from orchestrator.agent.integration_agent import read_integration_state

    tenant_id = uuid4()
    with _ctx(uuid4(), tenant_id):
        out = read_integration_state.func(tenant_id=str(tenant_id))  # type: ignore[attr-defined]
    assert out == {"phase": None, "current_connector_id": None, "pending_owner_input": None}


def test_read_integration_state_reflects_seeded_row(substrate):
    from orchestrator.agent.integration_agent import read_integration_state

    tid = _seed_tenant(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase, current_connector_id) "
            "VALUES (%s, 'phase_2_auth', 'shopify')",
            (tid,),
        )
    with _ctx(uuid4(), tid):
        out = read_integration_state.func(tenant_id=tid)  # type: ignore[attr-defined]
    assert out["phase"] == "phase_2_auth"
    assert out["current_connector_id"] == "shopify"


# --- check_oauth_status ---------------------------------------------------------------------


def test_check_oauth_status_false_then_true(substrate):
    from orchestrator.agent.integration_agent import check_oauth_status

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        before = check_oauth_status.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id="google_sheet"
        )
    assert before == {"connector_id": "google_sheet", "connected": False}

    _seed_oauth_token(substrate, tid, "google_sheet")
    with _ctx(uuid4(), tid):
        after = check_oauth_status.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id="google_sheet"
        )
    assert after == {"connector_id": "google_sheet", "connected": True}


# --- pull_sample (google_sheet branch) — MINOR 5 full coverage -------------------------------


def test_pull_sample_google_sheet_awaiting_picker_selection(substrate):
    """No spreadsheet_id on file yet (the owner hasn't finished the picker) — an honest
    incomplete-input state, never a failure."""
    from orchestrator.agent.integration_agent import pull_sample

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        out = pull_sample.func(tenant_id=tid, connector_id="google_sheet")  # type: ignore[attr-defined]
    assert out == {"connector_id": "google_sheet", "status": "awaiting_picker_selection", "row_count": 0}


def test_pull_sample_google_sheet_success_advances_phase_and_persists_columns(substrate, monkeypatch):
    from orchestrator.agent.integration_agent import pull_sample, read_integration_state
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
    from orchestrator.onboarding.shopify_onboarding import PHASE_SAMPLE, _validated_pending, _write_state

    tid = _seed_tenant(substrate)
    # Mirrors what the picker's own POST /select would have already persisted.
    picker_pending = _validated_pending(
        awaiting="sample_pull_pending",
        prompt_text="x",
        connector_id="google_sheet",
        metadata={"spreadsheet_id": "sheet-x", "tab_name": "Sheet1"},
    )
    _write_state(tid, phase=PHASE_SAMPLE, connector_id="google_sheet", pending=picker_pending)

    def _fake_pull_sample(self, tenant_id, spreadsheet_id, *, tab_name=""):
        assert spreadsheet_id == "sheet-x"
        assert tab_name == "Sheet1"
        return [{"Mobile": "9876500055", "Name": "Ravi K"}]

    monkeypatch.setattr(GoogleSheetConnector, "pull_sample", _fake_pull_sample)

    with _ctx(uuid4(), tid):
        out = pull_sample.func(tenant_id=tid, connector_id="google_sheet")  # type: ignore[attr-defined]
    assert out["row_count"] == 1
    assert sorted(out["column_names"]) == ["Mobile", "Name"]

    # A LATER, independent call sees the phase advanced + columns persisted (resume proof).
    with _ctx(uuid4(), tid):
        state = read_integration_state.func(tenant_id=tid)  # type: ignore[attr-defined]
    assert state["phase"] == "phase_4_field_mapping"
    metadata = state["pending_owner_input"]["metadata"]
    assert sorted(metadata["column_names"]) == ["Mobile", "Name"]
    # spreadsheet_id/tab_name carried forward, not clobbered.
    assert metadata["spreadsheet_id"] == "sheet-x"
    assert metadata["tab_name"] == "Sheet1"


def test_pull_sample_google_sheet_connector_error_never_advances_phase(substrate, monkeypatch):
    from orchestrator.agent.integration_agent import pull_sample
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
    from orchestrator.onboarding.shopify_onboarding import PHASE_SAMPLE, _validated_pending, _write_state

    tid = _seed_tenant(substrate)
    picker_pending = _validated_pending(
        awaiting="sample_pull_pending",
        prompt_text="x",
        connector_id="google_sheet",
        metadata={"spreadsheet_id": "sheet-x", "tab_name": "Sheet1"},
    )
    _write_state(tid, phase=PHASE_SAMPLE, connector_id="google_sheet", pending=picker_pending)

    def _broken_pull_sample(self, tenant_id, spreadsheet_id, *, tab_name=""):
        raise RuntimeError("Sheets API unavailable")

    monkeypatch.setattr(GoogleSheetConnector, "pull_sample", _broken_pull_sample)

    with _ctx(uuid4(), tid):
        out = pull_sample.func(tenant_id=tid, connector_id="google_sheet")  # type: ignore[attr-defined]
    assert out["connector_id"] == "google_sheet"
    assert out["status"] == "error"  # a connector failure, never needs_owner_input

    with psycopg.connect(substrate, autocommit=True) as conn:
        row = conn.execute(
            "SELECT phase FROM tenant_integration_state WHERE tenant_id = %s", (tid,)
        ).fetchone()
    assert row[0] == "phase_3_sample_pull"  # unchanged — never fabricated progress


# --- confirm_mapping ---------------------------------------------------------------------


def test_confirm_mapping_persists_and_resumes_across_independent_calls(substrate):
    """RULING 4 (resume): the confirmed mapping written by ONE tool call is read back correctly
    by a LATER, INDEPENDENT call (no shared Python state — a fresh DB read every time, exactly
    the fresh-thread-per-message discipline the real WhatsApp surface imposes)."""
    from orchestrator.agent.integration_agent import confirm_mapping, read_integration_state

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        out = confirm_mapping.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id="google_sheet", mapping={"Mobile": "phone"}
        )
    assert out == {"connector_id": "google_sheet", "confirmed": True, "field_count": 1}

    # A brand new "turn" (independent call, no state carried) reads the SAME confirmed mapping.
    with _ctx(uuid4(), tid):
        state = read_integration_state.func(tenant_id=tid)  # type: ignore[attr-defined]
    assert state["phase"] == "phase_4_field_mapping"
    metadata = state["pending_owner_input"]["metadata"]
    assert metadata["confirmed_mapping"] == {"Mobile": "phone"}


def test_confirm_mapping_adversarial_write_never_lands_on_foreign_tenant(substrate):
    """VT-603 tenancy: the MODEL supplies a foreign tenant; the write must land on the ambient
    CONTEXT tenant only."""
    from orchestrator.agent.integration_agent import confirm_mapping, read_integration_state

    tenant_a = _seed_tenant(substrate)
    tenant_b = _seed_tenant(substrate)
    with _ctx(uuid4(), tenant_a):
        confirm_mapping.func(  # type: ignore[attr-defined]
            tenant_id=tenant_b, connector_id="google_sheet", mapping={"Mobile": "phone"}
        )

    with _ctx(uuid4(), tenant_a):
        state_a = read_integration_state.func(tenant_id=tenant_a)  # type: ignore[attr-defined]
    with _ctx(uuid4(), tenant_b):
        state_b = read_integration_state.func(tenant_id=tenant_b)  # type: ignore[attr-defined]

    assert state_a["pending_owner_input"]["metadata"]["confirmed_mapping"] == {"Mobile": "phone"}
    assert state_b == {"phase": None, "current_connector_id": None, "pending_owner_input": None}


# --- commit_ingestion — RULING 3: proposal ONLY, never a customers write ----------------------


def test_commit_ingestion_records_proposal_never_writes_customers(substrate):
    from orchestrator.agent.integration_agent import commit_ingestion, confirm_mapping

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        confirm_mapping.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id="google_sheet", mapping={"Mobile": "phone"}
        )
        # confirm_mapping alone doesn't carry a spreadsheet_id — seed one directly (mirrors what
        # the picker's own POST /select would have already written).
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "UPDATE tenant_integration_state SET pending_owner_input = "
            "pending_owner_input || '{\"metadata\": {\"spreadsheet_id\": \"sheet-x\", "
            "\"tab_name\": \"Sheet1\", \"confirmed_mapping\": {\"Mobile\": \"phone\"}}}'::jsonb "
            "WHERE tenant_id = %s",
            (tid,),
        )

    with _ctx(uuid4(), tid):
        out = commit_ingestion.func(tenant_id=tid, connector_id="google_sheet")  # type: ignore[attr-defined]
    assert out["status"] == "proposal_recorded"

    with psycopg.connect(substrate, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pending_owner_input FROM tenant_integration_state WHERE tenant_id = %s", (tid,)
        ).fetchone()
        customer_count = conn.execute(
            "SELECT count(*) FROM customers WHERE tenant_id = %s", (tid,)
        ).fetchone()[0]
    assert row[0]["awaiting"] == "ingestion_commit_pending"
    assert row[0]["metadata"]["spreadsheet_id"] == "sheet-x"
    # VT-268 / RULING 3 — the agent TOOL never wrote a customer row. Only the SEPARATE,
    # non-agent execute_pending_ingestion_commit path does that (see test_commit.py).
    assert customer_count == 0


def test_commit_ingestion_google_sheet_without_spreadsheet_id_errors_not_needs_owner_input(
    substrate,
):
    """Package 5 rule: config/connector failure never reported as needs_owner_input."""
    from orchestrator.agent.integration_agent import commit_ingestion

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        out = commit_ingestion.func(tenant_id=tid, connector_id="google_sheet")  # type: ignore[attr-defined]
    assert out["status"] == "error"


# --- verify_connector ---------------------------------------------------------------------


def test_verify_connector_truthful_before_and_after_cadence(substrate):
    from orchestrator.agent.integration_agent import verify_connector

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        before = verify_connector.func(tenant_id=tid, connector_id="shopify")  # type: ignore[attr-defined]
    assert before["connected"] is False
    assert before["cadence"] is None

    _seed_oauth_token(substrate, tid, "shopify")
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_connector_status "
            "(tenant_id, connector_id, pull_cadence, next_scheduled_run, enabled) "
            "VALUES (%s, 'shopify', '0 3 * * *', now(), TRUE)",
            (tid,),
        )
    with _ctx(uuid4(), tid):
        after = verify_connector.func(tenant_id=tid, connector_id="shopify")  # type: ignore[attr-defined]
    assert after["connected"] is True
    assert after["cadence"] == "0 3 * * *"
