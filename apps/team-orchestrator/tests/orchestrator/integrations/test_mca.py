"""VT-449 — MCA Company/Director Master Data client-shape contract (injected transport, no live creds)."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

_REASON = "owner KYC onboarding identity verification"  # ≥20 chars (DPDP)


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    from orchestrator.integrations.methods import sandbox_kyc

    monkeypatch.setenv("SANDBOX_API_KEY", "key-abc")
    monkeypatch.setenv("SANDBOX_API_SECRET", "secret-xyz")
    sandbox_kyc._token = None
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


def test_company_master_data_parses_canonical_name_and_directors():
    from orchestrator.integrations.methods import mca

    req, calls = _recorder({
        "/authenticate": {"access_token": "TOK"},
        "/mca/company/master-data/search": {"data": {
            "company_master_data": {
                "cin": "U52609MH2020OPC344309",
                "company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
                "company_status": "Active", "active_compliance": "ACTIVE compliant",
                "registered_address": "MG Road, Mumbai", "paid_up_capital": "100000",
            },
            "directors": [{"name": "A Director", "din": "01234567", "designation": "Director"}],
        }},
    })
    res = mca.company_master_data("U52609MH2020OPC344309", reason=_REASON, request_fn=req)
    assert res.ok
    assert res.company_name == "RKECOM SERVICES (OPC) PRIVATE LIMITED"  # the canonical name-match anchor
    assert res.status == "Active" and res.paid_up_capital == "100000"
    assert len(res.directors) == 1 and res.directors[0]["din"] == "01234567"

    call = calls[1]
    assert call["path"] == "/mca/company/master-data/search"
    assert call["body"]["consent"] == "y" and call["body"]["id"] == "U52609MH2020OPC344309"
    assert call["body"]["reason"] == _REASON
    assert call["body"]["@entity"] == "in.co.sandbox.kyc.mca.master_data.request"
    assert call["headers"]["authorization"] == "TOK" and call["headers"]["x-api-version"] == "1.0.0"


def test_director_master_data_directs_cin_ownership_check():
    from orchestrator.integrations.methods import mca

    req, _ = _recorder({
        "/authenticate": {"access_token": "TOK"},
        "/mca/director/master-data/search": {"data": {
            "director_data": {"din": "01234567", "name": "A Director"},
            "company_data": [{"company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
                              "cin": "U52609MH2020OPC344309", "designation": "Director"}],
        }},
    })
    res = mca.director_master_data("01234567", reason=_REASON, request_fn=req)
    assert res.ok and res.name == "A Director"
    assert res.directs_cin("U52609MH2020OPC344309") is True   # VT-411 KYC ownership
    assert res.directs_cin("U99999XX9999XXX999999") is False


def test_mca_reason_too_short_fails_closed_no_call():
    from orchestrator.integrations.methods import mca

    def req(*_a):
        raise AssertionError("no MCA call with a too-short reason")

    assert mca.company_master_data("U52609MH2020OPC344309", reason="short", request_fn=req).ok is False


def test_mca_bad_id_fails_closed():
    from orchestrator.integrations.methods import mca

    req, _ = _recorder({"/authenticate": {"access_token": "TOK"}})
    assert mca.company_master_data("", reason=_REASON, request_fn=req).ok is False
    assert mca.director_master_data("", reason=_REASON, request_fn=req).ok is False
