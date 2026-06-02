"""VT-288 — hook links + ramp governor (Rule #15 canary).

Pure ramp-governor decisions + real-PG hook_links (mint→resolve→click, server-side
attribution) + the public /r redirect endpoint. Injected send seams (no network).
CL-422 synthetic.
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
    reason="DATABASE_URL not set — VT-288 substrate tests skipped",
)


# --- PURE: ramp governor ------------------------------------------------------

def test_ramp_cold_start_holds_until_min_sends():
    from orchestrator.integrations.ramp_governor import cold_start_cap, decide

    assert cold_start_cap() == 50
    d = decide(current_tier=0, engagement_rate=0.9, sends_in_window=5)
    assert d.action == "hold" and d.daily_cap == 50  # not enough sends to step yet


def test_ramp_promotes_on_engagement():
    from orchestrator.integrations.ramp_governor import decide

    d = decide(current_tier=0, engagement_rate=0.30, sends_in_window=40)
    assert d.action == "promote" and d.tier == 1 and d.daily_cap == 100


def test_ramp_holds_below_threshold():
    from orchestrator.integrations.ramp_governor import decide

    d = decide(current_tier=1, engagement_rate=0.15, sends_in_window=200)
    assert d.action == "hold" and d.daily_cap == 100


def test_ramp_demotes_on_quality_drop():
    from orchestrator.integrations.ramp_governor import decide

    d = decide(current_tier=3, engagement_rate=0.40, sends_in_window=300, quality_dropped=True)
    assert d.action == "demote" and d.tier == 2


def test_ramp_demotes_below_floor():
    from orchestrator.integrations.ramp_governor import decide

    d = decide(current_tier=2, engagement_rate=0.05, sends_in_window=300)
    assert d.action == "demote" and d.tier == 1


def test_ramp_caps_at_top_tier():
    from orchestrator.integrations.ramp_governor import decide

    d = decide(current_tier=4, engagement_rate=0.9, sends_in_window=999)
    assert d.action == "hold" and d.tier == 4  # already top


# --- DB: hook links -----------------------------------------------------------

@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str, *, wa_status: str = "live", number: str | None = None) -> str:
    num = number or f"+9180{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        tid = str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-288 test', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
            "VALUES (%s, %s, %s)",
            (tid, wa_status, num),
        )
    return tid


def test_mint_resolve_records_click_server_side(substrate):
    from orchestrator.integrations.hook_links import (
        mint_hook_link,
        resolve_and_record_click,
        wa_me_url,
    )

    num = "+919812345678"
    t = _tenant(substrate.dsn, wa_status="live", number=num)
    token = mint_hook_link(t, source="sms")
    res = resolve_and_record_click(token)
    assert res is not None
    assert res.tenant_id == UUID(t)            # tenant from server-side mapping, not URL text
    assert res.wa_number == num
    assert res.source == "sms"
    assert wa_me_url(num) == "https://wa.me/919812345678"
    # click recorded
    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        n = c.execute("SELECT click_count FROM hook_links WHERE token=%s", (token,)).fetchone()[0]
    assert n == 1


def test_resolve_unknown_token(substrate):
    from orchestrator.integrations.hook_links import resolve_and_record_click

    assert resolve_and_record_click("never-minted") is None
    assert resolve_and_record_click("") is None


def test_resolve_requires_live_waba(substrate):
    from orchestrator.integrations.hook_links import mint_hook_link, resolve_and_record_click

    t = _tenant(substrate.dsn, wa_status="verifying")  # not live
    token = mint_hook_link(t, source="email")
    assert resolve_and_record_click(token) is None  # can't redirect to a non-live WABA


# --- endpoint -----------------------------------------------------------------

@pytest.fixture(scope="module")
def app_ctx(substrate):
    os.environ["INTERNAL_API_SECRET"] = "vt288_secret"
    os.environ["HOOK_BASE_URL"] = "https://viabe.ai"
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield SimpleNamespace(dsn=substrate.dsn, client=client)


def test_redirect_endpoint_302(app_ctx):
    num = "+919800000123"
    t = _tenant(app_ctx.dsn, wa_status="live", number=num)
    mint = app_ctx.client.post(
        "/api/orchestrator/hooks/mint",
        json={"tenant_id": t, "source": "sms"},
        headers={"X-Internal-Secret": "vt288_secret"},
    )
    assert mint.status_code == 200, mint.text
    token = mint.json()["token"]
    assert mint.json()["url"] == f"https://viabe.ai/r/{token}"
    resp = app_ctx.client.get(f"/r/{token}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://wa.me/919800000123"


def test_redirect_unknown_404(app_ctx):
    resp = app_ctx.client.get("/r/nope-not-real", follow_redirects=False)
    assert resp.status_code == 404


def test_mint_requires_secret(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/hooks/mint", json={"tenant_id": str(uuid4())}
    )
    assert resp.status_code == 401
