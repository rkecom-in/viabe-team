"""VT-289 — google_sheet secured /setup + hardened /callback endpoint tests.

TestClient boots the real app. The security gates run without network:
- /setup requires INTERNAL_API_SECRET (401 without).
- /callback rejects a forged/unminted state (401) before any code exchange.
- /setup with the secret mints a real nonce + returns an authorize URL carrying it
  (and NOT the raw tenant_id).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-289 endpoint tests skipped",
)

_SECRET = "secret_test_vt289_ep"


@pytest.fixture(scope="module")
def app_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["INTERNAL_API_SECRET"] = _SECRET
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "gcid_test.apps.googleusercontent.com"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = (
        "https://viabe-team-dev.vercel.app/api/orchestrator/integrations/google/callback"
    )
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt289-ep-salt")

    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield SimpleNamespace(dsn=dsn, client=client)


def test_setup_requires_internal_secret(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/setup",
        json={"tenant_id": str(uuid4())},
    )
    assert resp.status_code == 401


def test_setup_with_secret_mints_nonce_url(app_ctx):
    tenant = str(uuid4())
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/google_sheet/setup",
        json={"tenant_id": tenant},
        headers={"X-Internal-Secret": _SECRET},
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    assert "accounts.google.com" in url
    assert "state=" in url
    assert tenant not in url  # VT-289: raw tenant_id never in the URL


def test_callback_rejects_forged_state(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/google/callback",
        params={"code": "authcode", "state": "forged-never-minted"},
    )
    assert resp.status_code == 401
