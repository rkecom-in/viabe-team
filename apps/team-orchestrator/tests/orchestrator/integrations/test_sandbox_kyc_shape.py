"""VT-361 — Sandbox client request-shape contract (no live creds / DB / egress needed).

#420 subagent BLOCKER: the wrong call shape was fully masked by fail-closed + the only real canary is
double-skipped in CI. This pins the documented two-step contract (POST /authenticate → token in the
`authorization` header WITHOUT Bearer → POST gstin/search with GSTIN in the BODY) via an injected
transport, so a regression is caught structurally without creds/network.
"""

from __future__ import annotations

import pytest

# Dep-less smoke collects ALL tests with only pytest+pyyaml — a module-level sandbox_kyc import pulls
# the orchestrator package chain (pydantic etc.) and fails COLLECTION. importorskip gates the module
# (skipped in smoke, runs in the full suite); the import itself is deferred into the fixture/tests.
pytest.importorskip("pydantic")


@pytest.fixture(autouse=True)
def _creds_and_clear_cache(monkeypatch):
    from orchestrator.integrations.methods import sandbox_kyc

    monkeypatch.setenv("SANDBOX_API_KEY", "key-abc")
    monkeypatch.setenv("SANDBOX_API_SECRET", "secret-xyz")
    sandbox_kyc._token = None  # reset the in-process token cache between tests
    sandbox_kyc._token_exp = 0.0
    yield
    sandbox_kyc._token = None
    sandbox_kyc._token_exp = 0.0


def _recorder(responses):
    calls = []

    def request_fn(method, path, headers, body):
        calls.append({"method": method, "path": path, "headers": headers, "body": body})
        return responses[path]

    return request_fn, calls


def test_two_step_auth_then_post_search_body_shape():
    from orchestrator.integrations.methods import sandbox_kyc

    req, calls = _recorder({
        "/authenticate": {"data": {"access_token": "TOK123"}},
        "/gst/compliance/public/gstin/search": {"data": {"legal_name": "RKECOM SERVICE (OPC) PRIVATE LIMITED", "status": "Active"}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)

    assert res.ok and res.is_active() and "RKECOM" in (res.authoritative_name() or "").upper()
    assert len(calls) == 2

    auth = calls[0]
    assert auth["method"] == "POST" and auth["path"] == "/authenticate"
    assert auth["headers"]["x-api-key"] == "key-abc"
    assert auth["headers"]["x-api-secret"] == "secret-xyz"
    assert auth["headers"]["x-api-version"] == "1.0"

    search = calls[1]
    assert search["method"] == "POST"  # POST, not GET
    assert search["path"] == "/gst/compliance/public/gstin/search"
    assert search["body"] == {"gstin": "27AAKCR3738B1ZE"}  # GSTIN in the BODY, not a query param
    assert search["headers"]["authorization"] == "TOK123"  # token, NO 'Bearer' prefix
    assert "Bearer" not in search["headers"]["authorization"]
    assert search["headers"]["x-api-key"] == "key-abc"
    assert search["headers"]["x-api-version"] == "1.0"
    assert "x-api-secret" not in search["headers"]  # secret only on /authenticate


def test_token_is_cached_across_calls():
    from orchestrator.integrations.methods import sandbox_kyc

    req, calls = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {"data": {"legal_name": "X", "status": "Active"}},
    })
    sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    # 2 lookups but only ONE /authenticate (token reused → no per-call auth cost).
    assert sum(1 for c in calls if c["path"] == "/authenticate") == 1


def test_401_triggers_reauth_once():
    from orchestrator.integrations.methods import sandbox_kyc

    class _401(Exception):
        def __init__(self):
            self.response = type("R", (), {"status_code": 401})()

    state = {"auth_calls": 0, "lookup_calls": 0}

    def req(method, path, headers, body):
        if path == "/authenticate":
            state["auth_calls"] += 1
            return {"data": {"access_token": f"TOK{state['auth_calls']}"}}
        state["lookup_calls"] += 1
        if state["lookup_calls"] == 1:
            raise _401()  # stale token → forces a re-auth + retry
        return {"data": {"legal_name": "X", "status": "Active"}}

    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    assert res.ok and res.is_active()
    assert state["auth_calls"] == 2 and state["lookup_calls"] == 2  # re-authed once, retried once


def test_no_creds_fails_closed(monkeypatch):
    from orchestrator.integrations.methods import sandbox_kyc

    monkeypatch.delenv("SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("SANDBOX_API_SECRET", raising=False)

    def req(*a):  # must never be called
        raise AssertionError("no vendor call without creds")

    assert sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req).ok is False
