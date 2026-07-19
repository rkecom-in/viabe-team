"""VT-608 RULING 3 — ``integrations.commit.execute_pending_ingestion_commit``: the deterministic,
non-agent code path that performs the REAL ingestion write the ``commit_ingestion`` agent tool
only ever PROPOSES (VT-268 fail-closed — see ``agent/integration_agent.py``'s own docstring).

Covers: a no-op when nothing is pending / the pending kind isn't a commit proposal; the real
Shopify + Google Sheets commit paths (mocked connector calls, real DB ingest + phase-advance);
an unsupported connector_id fails closed (never silently drops); a commit failure leaves the
phase at 'ingestion_commit_pending' (never fabricates a confirmed state); re-entry safety
(RULING 4); the fix-round's own three findings:
  - MAJOR 1 — same-turn arming identity: a mismatched/expired proposal is NEVER executed and is
    reverted to an honest, retryable state, never left to dangle or silently re-fire later.
  - MAJOR 2's counterpart lives in test_workflow.py (the outcome-gating is workflow.py's own).
  - CRITICAL 2 — the confirmed field mapping (not the alias guesser) drives the row transform.
  - MINOR 1/2 — the confirmation reports NEW customers (not the raw committed count) and is
    actually SENT.
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


def _seed_tenant(dsn: str, *, owner_phone: str | None = None) -> str:
    tid = str(uuid4())
    phone = owner_phone or f"+9198{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, owner_phone) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"vt608-commit-{tid[:8]}", phone),
        )
    return tid


_ARMED = "test-turn-1"


def _seed_pending_commit(
    dsn: str, tenant_id: str, *, connector_id: str, metadata: dict,
    armed_turn_id: str | None = _ARMED, expires_at: str | None = None,
) -> None:
    import json

    full_metadata = dict(metadata)
    if armed_turn_id is not None:
        full_metadata["armed_turn_id"] = armed_turn_id
    pending = {
        "awaiting": "ingestion_commit_pending",
        "prompt_text": "Committing your data now.",
        "connector_id": connector_id,
        "metadata": full_metadata,
        "expires_at": expires_at,
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


def _capture_sends(monkeypatch):
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod

    sent: list[str] = []
    monkeypatch.setattr(
        shopify_onboarding_mod, "_send", lambda recipient, text, **_kw: sent.append(text or "")
    )
    return sent


# --- no-op cases ---------------------------------------------------------------------------


def test_noop_when_no_state_row(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    assert execute_pending_ingestion_commit(tid, current_turn_id=_ARMED) is None


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
    assert execute_pending_ingestion_commit(tid, current_turn_id=_ARMED) is None
    # unchanged — never advanced past auth
    assert _read_state_row(substrate, tid)["phase"] == "phase_2_auth"


# --- shopify ---------------------------------------------------------------------------


def test_shopify_commit_executes_and_advances_to_confirmed(substrate, monkeypatch):
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    sent = _capture_sends(monkeypatch)
    _seed_pending_commit(substrate, tid, connector_id="shopify", metadata={})

    def _fake_pull_and_ingest(tenant_id, **kwargs):
        return {"orders_pulled": 3, "mapped": 3, "committed": 3, "sales_written": 3, "new_customers": 3}

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _fake_pull_and_ingest)

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
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

    # MINOR 2 — the confirmation was actually SENT (not just persisted).
    assert len(sent) == 1
    assert "3 new" in sent[0]


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

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert result["status"] == "completed"
    assert result["committed"] == 1
    assert result["new_customers"] == 1

    with psycopg.connect(substrate, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM customers WHERE tenant_id = %s", (tid,)
        ).fetchone()[0]
    assert n == 1

    state = _read_state_row(substrate, tid)
    assert state["phase"] == "phase_5_confirmed"


def test_google_sheet_commit_uses_confirmed_mapping_over_alias(substrate, monkeypatch):
    """CRITICAL 2 — a column the alias table would NEVER recognize ("cell#") lands correctly
    ONLY because the owner-confirmed mapping (persisted durably by commit_ingestion, read back
    via tenant_connector_status.field_mapping) drives the transform."""
    from orchestrator.integrations.commit import execute_pending_ingestion_commit
    from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector

    tid = _seed_tenant(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_connector_status (tenant_id, connector_id, field_mapping) "
            "VALUES (%s, 'google_sheet', %s::jsonb)",
            (tid, '{"cell#": "phone", "who": "customer_name"}'),
        )
    _seed_pending_commit(
        substrate, tid, connector_id="google_sheet",
        metadata={"spreadsheet_id": "sheet-x", "tab_name": "Sheet1"},
    )

    def _fake_pull_full(self, tenant_id, spreadsheet_id, *, since_row_index=0, since=None, tab_name=""):
        return [{"cell#": "9876500077", "who": "Meena S"}]

    monkeypatch.setattr(GoogleSheetConnector, "pull_full", _fake_pull_full)

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert result["status"] == "completed"
    assert result["committed"] == 1  # the alias table alone would have dropped this row (no anchor)


def test_google_sheet_commit_missing_spreadsheet_id_fails_without_advancing(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="google_sheet", metadata={})

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert result["status"] == "failed"
    # phase must NOT have advanced to confirmed on a failure — never fabricate success.
    assert _read_state_row(substrate, tid)["phase"] == "phase_4_field_mapping"


# --- unsupported connector / re-entry ---------------------------------------------------------


def test_unsupported_connector_fails_closed(substrate):
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="unknown_connector", metadata={})

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert result == {"status": "failed", "reason_code": "unsupported_connector"}
    assert _read_state_row(substrate, tid)["phase"] == "phase_4_field_mapping"


def test_reentry_after_success_is_a_safe_noop(substrate, monkeypatch):
    """RULING 4 — the same re-entry a process restart or a duplicate poll tick produces: calling
    the executor again once the phase has already advanced past the proposal must do nothing
    (never double-ingest)."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _capture_sends(monkeypatch)
    _seed_pending_commit(substrate, tid, connector_id="shopify", metadata={})

    calls = {"n": 0}

    def _fake_pull_and_ingest(tenant_id, **kwargs):
        calls["n"] += 1
        return {"committed": 1}

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _fake_pull_and_ingest)

    first = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert first["status"] == "completed"
    assert calls["n"] == 1

    second = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert second is None  # no-op — pending is no longer 'ingestion_commit_pending'
    assert calls["n"] == 1  # never re-ingested


# --- MAJOR 1: same-turn arming identity --------------------------------------------------------


def test_mismatched_turn_id_never_executes_reverts_honestly(substrate, monkeypatch):
    """THE adversarial proof: a proposal armed by turn A, polled by an UNRELATED later turn B,
    must NEVER execute — and must not dangle forever either (reverted to a retryable state)."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(
        substrate, tid, connector_id="shopify", metadata={}, armed_turn_id="turn-A"
    )

    def _forbidden_ingest(tenant_id, **kwargs):
        raise AssertionError("must NEVER execute a mismatched-turn proposal")

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _forbidden_ingest)

    result = execute_pending_ingestion_commit(tid, current_turn_id="turn-B")
    assert result == {"status": "stale_skipped", "connector_id": "shopify", "reason_code": "arming_mismatch"}

    state = _read_state_row(substrate, tid)
    assert state["phase"] == "phase_4_field_mapping"
    assert state["pending_owner_input"]["awaiting"] == "field_mapping_confirm"
    assert "armed_turn_id" not in state["pending_owner_input"]["metadata"]


def test_missing_armed_turn_id_never_executes(substrate, monkeypatch):
    """A pre-fix-round or malformed proposal with NO armed_turn_id at all must fail closed too
    (never treated as an implicit match)."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="shopify", metadata={}, armed_turn_id=None)

    def _forbidden_ingest(tenant_id, **kwargs):
        raise AssertionError("must NEVER execute a proposal with no armed_turn_id")

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _forbidden_ingest)

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert result["status"] == "stale_skipped"
    assert result["reason_code"] == "arming_mismatch"


def test_expired_proposal_never_executes(substrate, monkeypatch):
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(
        substrate, tid, connector_id="shopify", metadata={},
        armed_turn_id=_ARMED, expires_at="2020-01-01T00:00:00+00:00",
    )

    def _forbidden_ingest(tenant_id, **kwargs):
        raise AssertionError("must NEVER execute an expired proposal")

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _forbidden_ingest)

    result = execute_pending_ingestion_commit(tid, current_turn_id=_ARMED)
    assert result == {"status": "stale_skipped", "connector_id": "shopify", "reason_code": "expired"}
    assert _read_state_row(substrate, tid)["pending_owner_input"]["awaiting"] == "field_mapping_confirm"


def test_transient_failure_then_later_unrelated_turn_does_not_reingest(substrate, monkeypatch):
    """MAJOR 1's own headline scenario: a commit fails transiently (proposal stays
    ingestion_commit_pending, matching the EXISTING failure-handling contract); a LATER,
    UNRELATED turn's poll must NOT blindly re-fire the full ingest."""
    import orchestrator.onboarding.shopify_onboarding as shopify_onboarding_mod
    from orchestrator.integrations.commit import execute_pending_ingestion_commit

    tid = _seed_tenant(substrate)
    _seed_pending_commit(substrate, tid, connector_id="shopify", metadata={}, armed_turn_id="turn-A")

    def _flaky_ingest(tenant_id, **kwargs):
        raise RuntimeError("transient connector error")

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _flaky_ingest)

    first = execute_pending_ingestion_commit(tid, current_turn_id="turn-A")
    assert first["status"] == "failed"
    # unchanged — the EXISTING failure contract (still ingestion_commit_pending, same armed turn).
    assert _read_state_row(substrate, tid)["pending_owner_input"]["awaiting"] == "ingestion_commit_pending"

    calls = {"n": 0}

    def _would_succeed_now(tenant_id, **kwargs):
        calls["n"] += 1
        return {"committed": 1}

    monkeypatch.setattr(shopify_onboarding_mod, "pull_and_ingest_shopify", _would_succeed_now)

    second = execute_pending_ingestion_commit(tid, current_turn_id="turn-B-days-later")
    assert second["status"] == "stale_skipped"
    assert calls["n"] == 0  # never re-ingested off an unrelated turn
