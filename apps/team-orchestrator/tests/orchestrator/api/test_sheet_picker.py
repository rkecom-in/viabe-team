"""VT-608 RULING 2 — the Google Sheets picker backend (``api/sheet_picker.py``).

WA-in-app-browser link-out per CL-443: after OAuth, the owner taps a link into a minimal
team-web page (a follow-up row, noted in the VT-608 report) that calls these three
INTERNAL_API_SECRET-guarded endpoints server-side. No manual credential paste (CL-421); no raw
sheet row content ever passes through these endpoints (list/select only).

VT-608 fix round additions:
  - MINOR 3 — spreadsheet_id / tab_name are VALIDATED before anything is persisted or sent to
    Google (a bad id/name fails 400, never reaches the A1-range grammar or the Sheets API URL).
  - MINOR 4 — a duplicate/out-of-order POST /select must never REGRESS a tenant who has already
    moved past the picker step; a mismatched-tenant write must never cross tenants.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-608 sheet_picker endpoint tests skipped",
)

_SECRET = "secret_test_vt608_picker"
# A plausible (well-formed) Drive file id — long enough to pass MINOR 3's validation without
# needing a real Google credential (these tests never reach the live Drive/Sheets API).
_VALID_SPREADSHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.fixture(scope="module")
def app_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["INTERNAL_API_SECRET"] = _SECRET
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt608-picker-salt")

    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield SimpleNamespace(dsn=dsn, client=client)


def _seed_tenant(dsn: str) -> str:
    import psycopg

    tid = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt608-picker-{tid[:8]}"),
        )
    return tid


def _read_state(dsn: str, tenant_id: str) -> dict:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT phase, pending_owner_input FROM tenant_integration_state WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    return {"phase": row[0], "pending_owner_input": row[1]} if row is not None else {}


# --- auth gate on all three endpoints ---------------------------------------------------------


def test_list_spreadsheets_requires_internal_secret(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google_sheet/spreadsheets",
        params={"tenant_id": str(uuid4())},
    )
    assert resp.status_code == 401


def test_list_tabs_requires_internal_secret(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google_sheet/tabs",
        params={"tenant_id": str(uuid4()), "spreadsheet_id": _VALID_SPREADSHEET_ID},
    )
    assert resp.status_code == 401


def test_select_spreadsheet_requires_internal_secret(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": str(uuid4()), "spreadsheet_id": _VALID_SPREADSHEET_ID, "tab_name": "Sheet1"},
    )
    assert resp.status_code == 401


# --- list_spreadsheets / list_tabs — connector errors surface as a clean 502, not a 500 --------


def test_list_spreadsheets_no_oauth_token_fails_closed_502(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google_sheet/spreadsheets",
        params={"tenant_id": str(uuid4())},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 502


def test_list_tabs_no_oauth_token_fails_closed_502(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google_sheet/tabs",
        params={"tenant_id": str(uuid4()), "spreadsheet_id": _VALID_SPREADSHEET_ID},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 502


def test_list_spreadsheets_bad_tenant_id_400(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google_sheet/spreadsheets",
        params={"tenant_id": "not-a-uuid"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_list_tabs_rejects_malformed_spreadsheet_id(app_ctx):
    """MINOR 3 — a malformed spreadsheet_id (too short / bad characters) is rejected 400 BEFORE
    any Sheets API call is attempted."""
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google_sheet/tabs",
        params={"tenant_id": str(uuid4()), "spreadsheet_id": "../etc/passwd"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400


# --- select_spreadsheet — persists the phase + metadata -----------------------------------------


def test_select_spreadsheet_requires_both_fields(app_ctx):
    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": "", "tab_name": "Sheet1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_select_spreadsheet_rejects_malformed_spreadsheet_id(app_ctx):
    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": "bad id!", "tab_name": "Sheet1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_select_spreadsheet_rejects_malformed_tab_name(app_ctx):
    """MINOR 3 — a tab name carrying a Sheets-forbidden character (also the exact character
    class that would otherwise break the A1-range grammar) is rejected, never persisted."""
    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": _VALID_SPREADSHEET_ID, "tab_name": "Sheet/1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400
    assert _read_state(app_ctx.dsn, tid) == {}


def test_select_spreadsheet_persists_sample_pending_phase(app_ctx):
    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": _VALID_SPREADSHEET_ID, "tab_name": "Sheet1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["phase"] == "phase_3_sample_pull"

    state = _read_state(app_ctx.dsn, tid)
    assert state["phase"] == "phase_3_sample_pull"
    pending = state["pending_owner_input"]
    # VT-608 fix over the dead-builder draft: a machine waypoint, not a reused owner-question kind.
    assert pending["awaiting"] == "sample_pull_pending"
    assert pending["metadata"]["spreadsheet_id"] == _VALID_SPREADSHEET_ID
    assert pending["metadata"]["tab_name"] == "Sheet1"


def test_select_spreadsheet_apostrophe_tab_name_accepted_and_escaped_downstream(app_ctx):
    """A tab name with an apostrophe is a VALID Sheets tab name (only [ ] : * ? / \\ are
    forbidden) — accepted here; MINOR 3's escaping lives in the connector's own A1-range builder
    (test_sheets_readonly_lock.py / the connector's own unit tests), not this endpoint."""
    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": _VALID_SPREADSHEET_ID, "tab_name": "Bob's Data"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 200, resp.text


def test_select_spreadsheet_does_not_regress_a_tenant_past_the_picker_step(app_ctx):
    """MINOR 4 — a duplicate/out-of-order POST must never regress a tenant already at
    phase_4_field_mapping (or later) back to phase_3_sample_pull."""
    import psycopg

    tid = _seed_tenant(app_ctx.dsn)
    with psycopg.connect(app_ctx.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase, current_connector_id, "
            "pending_owner_input) VALUES (%s, 'phase_4_field_mapping', 'google_sheet', "
            "'{\"awaiting\": \"field_mapping_confirm\", \"prompt_text\": \"x\", "
            "\"connector_id\": \"google_sheet\", \"metadata\": {\"confirmed_mapping\": "
            "{\"Mobile\": \"phone\"}}}'::jsonb)",
            (tid,),
        )

    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": _VALID_SPREADSHEET_ID, "tab_name": "Sheet2"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is False
    assert body["phase"] == "phase_4_field_mapping"

    # unchanged — the confirmed mapping from earlier must still be intact.
    state = _read_state(app_ctx.dsn, tid)
    assert state["phase"] == "phase_4_field_mapping"
    assert state["pending_owner_input"]["metadata"]["confirmed_mapping"] == {"Mobile": "phone"}


def test_select_spreadsheet_never_writes_a_different_tenant(app_ctx):
    """A mismatched-tenant regression lock: tenant A's POST must never appear under tenant B."""
    tenant_a = _seed_tenant(app_ctx.dsn)
    tenant_b = _seed_tenant(app_ctx.dsn)

    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tenant_a, "spreadsheet_id": _VALID_SPREADSHEET_ID, "tab_name": "Sheet1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 200, resp.text

    assert _read_state(app_ctx.dsn, tenant_a)["phase"] == "phase_3_sample_pull"
    assert _read_state(app_ctx.dsn, tenant_b) == {}
