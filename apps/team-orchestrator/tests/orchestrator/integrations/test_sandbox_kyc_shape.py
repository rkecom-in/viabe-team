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

    # Real Sandbox lookup shape: the GST record is nested under data.data (verified by the VT-361
    # live canary against Fazal's GSTIN), NOT data — and uses the GST short field names lgnm/tradeNam/sts.
    req, calls = _recorder({
        "/authenticate": {"data": {"access_token": "TOK123"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": {"lgnm": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "tradeNam": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "sts": "Active", "gstin": "27AAKCR3738B1ZE"}, "status_cd": "1"}},
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
    assert search["headers"]["x-api-version"] == "1.0.0"  # VT-409: search uses 1.0.0 (auth stays 1.0)
    assert "x-api-secret" not in search["headers"]  # secret only on /authenticate


def test_vt409_auth_prefers_top_level_token_not_nested():
    """VT-409 regression: /authenticate returns the token at BOTH top-level access_token (WORKS) AND
    nested data.access_token (500s on search). We MUST send the TOP-LEVEL one. The structural guard so
    this exact drift can't silently return the dud token again (mirrors the data.data depth guard)."""
    from orchestrator.integrations.methods import sandbox_kyc

    req, calls = _recorder({
        # BOTH tokens present, DIFFERENT values — only the top-level one is the working token.
        "/authenticate": {"code": 200, "access_token": "TOP_WORKS", "data": {"access_token": "NESTED_500s"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": {"lgnm": "RKECOM", "sts": "Active"}, "status_cd": "1"}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)

    assert res.ok and res.is_active()
    search = calls[1]
    assert search["headers"]["authorization"] == "TOP_WORKS"  # NOT "NESTED_500s"
    assert search["headers"]["x-api-version"] == "1.0.0"  # VT-409: search uses 1.0.0


def test_vt409_auth_falls_back_to_nested_when_no_top_level():
    """Back-compat: a response that omits the top-level token still resolves via data.access_token."""
    from orchestrator.integrations.methods import sandbox_kyc

    req, calls = _recorder({
        "/authenticate": {"data": {"access_token": "ONLY_NESTED"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": {"lgnm": "RKECOM", "sts": "Active"}, "status_cd": "1"}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    assert res.ok
    assert calls[1]["headers"]["authorization"] == "ONLY_NESTED"


def test_token_is_cached_across_calls():
    from orchestrator.integrations.methods import sandbox_kyc

    req, calls = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": {"lgnm": "X", "sts": "Active"}}},
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
        return {"data": {"data": {"lgnm": "X", "sts": "Active"}}}

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


def test_unparseable_200_fails_closed():
    """A 200 whose body we can't parse into a name/status must read ok=False, NEVER fake-verified.
    This is the latent bug the VT-361 live canary exposed: the old single-level read mapped the real
    (data.data-nested) body to all-None yet returned ok=True."""
    from orchestrator.integrations.methods import sandbox_kyc

    req, _ = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {"data": {"unexpected": "shape"}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    assert res.ok is False and res.authoritative_name() is None


# ----------------------------------------------------------------- VT-407 widen


def test_widen_parses_all_rich_fields():
    """A full data.data record (pradr/ctb/nba/rgdt/adadr) → every VT-407 field parses; name+status
    still drive ok/active (the verified signal is unchanged, the rest is extra context)."""
    from orchestrator.integrations.methods import sandbox_kyc

    record = {
        "lgnm": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "tradeNam": "RKECOM",
        "sts": "Active",
        "ctb": "Private Limited Company",
        "rgdt": "01/07/2017",
        "nba": ["Retail Business", "Supplier of Services"],
        "pradr": {
            "addr": {
                "bno": "12", "bnm": "Galaxy Tower", "st": "MG Road", "loc": "Andheri",
                "dst": "Mumbai", "stcd": "Maharashtra", "pncd": "400001",
                "lt": "19.0760", "lg": "72.8777",
            },
            "ntr": "Office",
        },
        "adadr": [
            {"addr": {"bno": "7", "st": "Link Road", "dst": "Pune", "stcd": "Maharashtra", "pncd": "411001"}},
            {"addr": {"bnm": "Warehouse 3", "loc": "MIDC", "dst": "Thane", "stcd": "Maharashtra"}},
        ],
    }
    req, _ = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": record, "status_cd": "1"}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)

    assert res.ok and res.is_active()
    assert res.legal_name == "RKECOM SERVICES (OPC) PRIVATE LIMITED"
    assert res.trade_name == "RKECOM"
    assert res.constitution == "Private Limited Company"
    assert res.is_proprietorship() is False
    assert res.registration_date == "01/07/2017"
    assert res.nature_of_business == ["Retail Business", "Supplier of Services"]
    assert res.geo_lat == "19.0760" and res.geo_lng == "72.8777"
    # principal address composed in postal order, empties dropped
    assert res.principal_address == "12, Galaxy Tower, MG Road, Andheri, Mumbai, Maharashtra, 400001"
    assert res.additional_addresses == (
        "7, Link Road, Pune, Maharashtra, 411001",
        "Warehouse 3, MIDC, Thane, Maharashtra",
    )
    # business_fields packages the extras; legal_name is NOT in it (caller gates that)
    bf = res.business_fields()
    assert "legal_name" not in bf
    assert bf["trade_name"] == "RKECOM"
    assert bf["constitution"] == "Private Limited Company"
    assert bf["principal_address"].startswith("12, Galaxy Tower")
    assert bf["nature_of_business"] == ["Retail Business", "Supplier of Services"]
    assert bf["additional_addresses"] == list(res.additional_addresses)
    assert bf["registration_date"] == "01/07/2017"
    assert bf["geo_lat"] == "19.0760" and bf["geo_lng"] == "72.8777"


def test_principal_address_drops_empty_and_comma_only_subfields():
    """VT-407 minor — the live RKECOM canary returned ``flno: ','`` (a floor-number subfield that is
    ONLY a comma) plus an empty ``landMark`` / empty geo. The naive join surfaced a cosmetic leading
    ``',, A/403, ...'``. Comma-only and empty subfields must drop out so there are NO leading or
    doubled commas; real internal commas inside a building name are preserved."""
    from orchestrator.integrations.methods import sandbox_kyc

    record = {
        "lgnm": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "tradeNam": "RKECOM",
        "sts": "Active",
        "ctb": "Private Limited Company",
        "pradr": {
            "addr": {
                "flno": ",",  # comma-only → must drop (the canary's actual leading-comma source)
                "bno": "A/403",
                "bnm": "DHEERAJ HERITAGE RESI,PLOT-E2 DAULAT NGR CHS",  # real internal comma — kept
                "st": "SANTACRUZ WEST NEAR JUHU",
                "loc": "MUMBAI",
                "landMark": "",  # empty → drop
                "dst": "Mumbai",
                "stcd": "Maharashtra",
                "pncd": "400054",
                "lt": "",  # geocodelvl: NA on RKECOM → geo genuinely absent
                "lg": "",
            },
        },
    }
    req, _ = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": record, "status_cd": "1"}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)

    assert res.ok and res.is_active()
    assert res.principal_address == (
        "A/403, DHEERAJ HERITAGE RESI,PLOT-E2 DAULAT NGR CHS, "
        "SANTACRUZ WEST NEAR JUHU, MUMBAI, Mumbai, Maharashtra, 400054"
    )
    # no leading / doubled commas anywhere
    assert not res.principal_address.startswith(",")
    assert ", ," not in res.principal_address and ",," not in res.principal_address
    # empty lt/lg → geo degrades to None (correct; not a key miss)
    assert res.geo_lat is None and res.geo_lng is None


def test_compose_address_helper_unit():
    """Direct ``_compose_address`` units: comma-only / whitespace-only subfields drop; an addr with
    no usable subfields → None; non-dict → None."""
    from orchestrator.integrations.methods import sandbox_kyc

    assert sandbox_kyc._compose_address({"flno": ",", "bno": "A/403", "st": "MG Road"}) == "A/403, MG Road"
    assert sandbox_kyc._compose_address({"flno": ",", "bno": "  ", "bnm": ", ,"}) is None
    assert sandbox_kyc._compose_address({}) is None
    assert sandbox_kyc._compose_address(None) is None


def test_widen_minimal_record_leaves_rich_fields_empty_but_ok_true():
    """A minimal record (only lgnm/sts) still verifies (ok=True); every rich field → None/[] and
    business_fields() carries no rich extras. The widen NEVER weakens the fail-closed contract."""
    from orchestrator.integrations.methods import sandbox_kyc

    req, _ = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {"data": {"data": {"lgnm": "Solo Co", "sts": "Active"}}},
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)

    assert res.ok and res.is_active()
    assert res.principal_address is None
    assert res.geo_lat is None and res.geo_lng is None
    assert res.constitution is None
    assert res.registration_date is None
    assert res.nature_of_business == []
    assert res.additional_addresses == ()
    assert res.is_proprietorship() is False  # no constitution → not flagged proprietorship
    # business_fields has no rich extras (trade_name absent here too)
    assert res.business_fields() == {}


def test_nature_of_business_accepts_single_string():
    """``nba`` is normally a list but a single string is tolerated → wrapped to a one-item list."""
    from orchestrator.integrations.methods import sandbox_kyc

    req, _ = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {
            "data": {"data": {"lgnm": "X", "sts": "Active", "nba": "Retail Business"}}
        },
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    assert res.nature_of_business == ["Retail Business"]


def test_proprietorship_constitution_flags_pii():
    """ctb='Proprietorship' → is_proprietorship() True; lgnm is then a person and the caller must
    NOT promote it (business_fields still excludes legal_name regardless)."""
    from orchestrator.integrations.methods import sandbox_kyc

    req, _ = _recorder({
        "/authenticate": {"data": {"access_token": "TOK"}},
        "/gst/compliance/public/gstin/search": {
            "data": {"data": {"lgnm": "Ramesh Kumar", "sts": "Active", "ctb": "Proprietorship"}}
        },
    })
    res = sandbox_kyc.search_gstin("27AAKCR3738B1ZE", request_fn=req)
    assert res.is_proprietorship() is True
    assert res.legal_name == "Ramesh Kumar"
    assert "legal_name" not in res.business_fields()
