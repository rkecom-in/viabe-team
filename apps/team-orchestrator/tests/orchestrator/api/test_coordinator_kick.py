"""VT-431 — coordinator kick endpoint tests.

Mounts only the coordinator_kick router (no DB, no DBOS — kick_coordinator is
monkeypatched). Asserts:
  1. 401 without the X-Internal-Secret header;
  2. 401 with a wrong secret;
  3. with the correct secret, calls kick_coordinator with the tenant_id and returns
     the CoordinatorSweepSummary counters as JSON;
  4. 500 on kick_coordinator exception (PII-safe error body).

No real sends, no real coordinator activity.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_SECRET = "vt431-test-internal-secret"
_HDR = {"X-Internal-Secret": _SECRET}
_TENANT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client(monkeypatch):
    from orchestrator.api.coordinator_kick import router

    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1 + 2 — internal-secret gate
# ---------------------------------------------------------------------------


def test_kick_requires_internal_secret(client):
    """No header → 401."""
    r = client.post(
        "/api/orchestrator/coordinator/kick", json={"tenant_id": _TENANT_ID}
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"


def test_kick_rejects_wrong_secret(client):
    """Wrong header value → 401."""
    r = client.post(
        "/api/orchestrator/coordinator/kick",
        json={"tenant_id": _TENANT_ID},
        headers={"X-Internal-Secret": "not-the-secret"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3 — with correct secret → calls kick_coordinator + returns summary as JSON
# ---------------------------------------------------------------------------


def test_kick_calls_coordinator_and_returns_summary(client, monkeypatch):
    """Good secret + valid tenant_id → kick_coordinator called, summary JSON returned."""
    from orchestrator.agents.coordinator import CoordinatorSweepSummary

    captured = {}

    def fake_kick(tenant_id, **kwargs):
        captured["tenant_id"] = tenant_id
        return CoordinatorSweepSummary(
            swept_at_utc="2026-06-29T10:00:00+00:00",
            tenants_scanned=1,
            dispatched=1,
        )

    monkeypatch.setattr(
        "orchestrator.agents.coordinator.kick_coordinator", fake_kick
    )

    r = client.post(
        "/api/orchestrator/coordinator/kick",
        json={"tenant_id": _TENANT_ID},
        headers=_HDR,
    )
    assert r.status_code == 200

    body = r.json()
    assert body["tenants_scanned"] == 1
    assert body["dispatched"] == 1
    assert body["swept_at_utc"] == "2026-06-29T10:00:00+00:00"
    # PII-safe: counters only — no names/phones in the summary dataclass
    assert "tenant_failures" in body
    assert "global_freeze" in body

    # Verify tenant_id was passed through correctly
    assert str(captured["tenant_id"]) == _TENANT_ID


# ---------------------------------------------------------------------------
# 4 — kick_coordinator exception → 500 (PII-safe)
# ---------------------------------------------------------------------------


def test_kick_500_on_coordinator_error(client, monkeypatch):
    """kick_coordinator raises → 500 with PII-safe error code."""

    def boom(tenant_id, **kwargs):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr("orchestrator.agents.coordinator.kick_coordinator", boom)

    r = client.post(
        "/api/orchestrator/coordinator/kick",
        json={"tenant_id": _TENANT_ID},
        headers=_HDR,
    )
    assert r.status_code == 500
    body = r.json()
    # PII-safe: only a code, not the exception message
    assert body["detail"]["code"] == "coordinator_error"


# ---------------------------------------------------------------------------
# 5 — missing tenant_id → 422 (pydantic validation)
# ---------------------------------------------------------------------------


def test_kick_missing_tenant_id_422(client):
    """Body without tenant_id → 422 before the endpoint logic runs."""
    r = client.post(
        "/api/orchestrator/coordinator/kick",
        json={},
        headers=_HDR,
    )
    assert r.status_code == 422
