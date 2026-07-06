"""VT-608 RULING 3 — ``integrations.commit.execute_pending_ingestion_commit``: the deterministic,
non-agent code path that performs the REAL ingestion write the ``commit_ingestion`` agent tool
only ever PROPOSES (VT-268 fail-closed — see ``agent/integration_agent.py``'s own docstring).

Covers: a no-op when nothing is pending / the pending kind isn't a commit proposal; the real
Shopify + Google Sheets commit paths (mocked connector calls, real DB ingest + phase-advance);
an unsupported connector_id fails closed (never silently drops); a commit failure leaves the
phase at 'ingestion_commit_pending' (never fabricates a confirmed state); and — RULING 4's own
resume proof — calling the executor AGAIN after a successful commit is a safe no-op (the SAME
deterministic re-entry a process restart or a duplicate poll tick would produce).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-608 commit executor tests skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt608-commit-salt")

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
            (tid, f"vt608-commit-{tid[:8]}"),
        )
    return tid


def _seed_pending_commit(dsn: str, tenant_id: str, *, connector_id: str, metadata: dict) -> None:
    import json

    pending = {
        "awaiting": "ingestion_commit_pending",
        "prompt_text": "Committing your data now.",
        "connector_id": connector_id,
        "metadata": metadata,
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state "
            "(tenant_id, phase, current_connector_id, pending_owner_input) "
            "VALUES (%s, 'phase_4_field_mapping', %s, %s::jsonb) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "phase = EXCLUDED.phase, current_connector_id = EXCLUDED.current_connector_id, "
            "pending_owner_input = EXCLUDED.pending_owner_input",
            (tenant_id, connector_id, json.dumps(pending)),
        )


def _read_state_row(dsn: str, tenant_id: str) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT phase, pending_owner_input FROM tenant_integration_state WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    return {"phase": row[0], "pending_owner_input": row[1]}


# --- no-op cases ---------------------------------------------------------------------------


def test_noop_when_no_state_row(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    assert execute_pending_ingestion_commit(tid) is None


def test_noop_when_pending_is_not_commit_kind(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase, current_connector_id, "
            "pending_owner_input) VALUES (%s, 'phase_2_auth', 'shopify', "
            "'{\"awaiting\": \"oauth_completion\", \"prompt_text\": \"x\", "
            "\"connector_id\": \"shopify\", \"metadata\": {}}'::jsonb)",
            (tid,),
        )
    assert execute_pending_ingestion_commit(tid) is None
    # unchanged — never advanced past auth
    assert _read_state_row(substrate, tid)["phase"] == "phase_2_auth"


# --- shopify ---------------------------------------------------------------------------


def test_shopify_commit_executes_and_advances_to_confirmed(substrate, monkeypatch):
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="shopify", metadata={})

    def _fake_pull_and_ingest(tenant_id, **kwargs):
        return {"orders_pulled": 3, "mapped": 3, "committed": 3, "sales_written": 3}

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _fake_pull_and_ingest)

    result = execute_pending_ingestion_commit(tid)
    assert result["status"] == "completed"
    assert result["committed"] == 3

    state = _read_state_row(substrate, tid)
    assert state["phase"] == "phase_5_confirmed"
    assert state["pending_owner_input"]["awaiting"] == "cadence_choice"

    with psycopg.connect(substrate, autocommit=True) as conn:
        cadence_row = conn.execute(
            "SELECT enabled FROM tenant_connector_status WHERE tenant_id = %s AND connector_id = 'shopify'",
            (tid,),
        ).fetchone()
    assert cadence_row is not None and cadence_row[0] is True  # default cadence auto-scheduled


# --- google_sheet ---------------------------------------------------------------------------


def test_google_sheet_commit_lands_real_customer_row(substrate, monkeypatch):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector

    tid = _seed_tenant(substrate)
    _seed_pending_commit(
        substrate, tid, connector_id="google_sheet",
        metadata={"spreadsheet_id": "sheet-x", "tab_name": "Sheet1"},
    )

    def _fake_pull_full(self, tenant_id, spreadsheet_id, *, since_row_index=0, since=None, tab_name=""):
        assert spreadsheet_id == "sheet-x"
        assert tab_name == "Sheet1"
        return [{"Mobile": "9876500055", "Name": "Ravi K"}]

    monkeypatch.setattr(GoogleSheetConnector, "pull_full", _fake_pull_full)

    result = execute_pending_ingestion_commit(tid)
    assert result["status"] == "completed"
    assert result["committed"] == 1

    with psycopg.connect(substrate, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM customers WHERE tenant_id = %s", (tid,)
        ).fetchone()[0]
    assert n == 1

    state = _read_state_row(substrate, tid)
    assert state["phase"] == "phase_5_confirmed"


def test_google_sheet_commit_missing_spreadsheet_id_fails_without_advancing(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="google_sheet", metadata={})

    result = execute_pending_ingestion_commit(tid)
    assert result["status"] == "failed"
    # phase must NOT have advanced to confirmed on a failure — never fabricate success.
    assert _read_state_row(substrate, tid)["phase"] == "phase_4_field_mapping"


# --- unsupported connector / re-entry ---------------------------------------------------------


def test_unsupported_connector_fails_closed(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="unknown_connector", metadata={})

    result = execute_pending_ingestion_commit(tid)
    assert result == {"status": "failed", "reason_code": "unsupported_connector"}
    assert _read_state_row(substrate, tid)["phase"] == "phase_4_field_mapping"


def test_reentry_after_success_is_a_safe_noop(substrate, monkeypatch):
    """RULING 4 — the same re-entry a process restart or a duplicate poll tick produces: calling
    the executor again once the phase has already advanced past the proposal must do nothing
    (never double-ingest)."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="shopify", metadata={})

    calls = {"n": 0}

    def _fake_pull_and_ingest(tenant_id, **kwargs):
        calls["n"] += 1
        return {"committed": 1}

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _fake_pull_and_ingest)

    first = execute_pending_ingestion_commit(tid)
    assert first["status"] == "completed"
    assert calls["n"] == 1

    second = execute_pending_ingestion_commit(tid)
    assert second is None  # no-op — pending is no longer 'ingestion_commit_pending'
    assert calls["n"] == 1  # never re-ingested
