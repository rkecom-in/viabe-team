"""VT-608 RULING 2 — the Google Sheets picker backend (``api/sheet_picker.py``).

WA-in-app-browser link-out per CL-443: after OAuth, the owner taps a link into a minimal
team-web page (a follow-up row, noted in the VT-608 report) that calls these three
INTERNAL_API_SECRET-guarded endpoints server-side. No manual credential paste (CL-421); no raw
sheet row content ever passes through these endpoints (list/select only).
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
        params={"tenant_id": str(uuid4()), "spreadsheet_id": "sheet-x"},
    )
    assert resp.status_code == 401


def test_select_spreadsheet_requires_internal_secret(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": str(uuid4()), "spreadsheet_id": "sheet-x", "tab_name": "Sheet1"},
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
        params={"tenant_id": str(uuid4()), "spreadsheet_id": "sheet-x"},
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


# --- select_spreadsheet — persists the phase + metadata -----------------------------------------


def test_select_spreadsheet_requires_both_fields(app_ctx):
    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": "", "tab_name": "Sheet1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_select_spreadsheet_persists_sample_pending_phase(app_ctx):
    import psycopg

    tid = _seed_tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/select",
        json={"tenant_id": tid, "spreadsheet_id": "sheet-x", "tab_name": "Sheet1"},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["phase"] == "phase_3_sample_pull"

    with psycopg.connect(app_ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT phase, pending_owner_input FROM tenant_integration_state WHERE tenant_id = %s",
            (tid,),
        ).fetchone()
    assert row is not None
    assert row[0] == "phase_3_sample_pull"
    pending = row[1]
    # VT-608 fix over the dead-builder draft: a machine waypoint, not a reused owner-question kind.
    assert pending["awaiting"] == "sample_pull_pending"
    assert pending["metadata"]["spreadsheet_id"] == "sheet-x"
    assert pending["metadata"]["tab_name"] == "Sheet1"
