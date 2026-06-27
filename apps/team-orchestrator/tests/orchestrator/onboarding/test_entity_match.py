"""VT-406 — entity-match unit tests (injected fns; no network/creds/DB)."""

from __future__ import annotations

from typing import Any

from orchestrator.onboarding import entity_match

_VALID_GSTIN = "29ABCDE1234F1Z5"  # 2 state + PAN(ABCDE1234F) + entity '1' + 'Z' + checksum '5'


def test_fetch_candidates_extracts_gstin_from_web_results() -> None:
    def search_fn(query: str) -> list[dict[str, Any]]:
        assert "GST number" in query
        return [
            {"title": "Sundaram Book Store", "description": f"GSTIN {_VALID_GSTIN} active", "url": "x"},
            {"title": "No gstin here", "description": "nothing"},
        ]

    cands = entity_match.fetch_candidates("Sundaram Book Store", "Chennai", search_fn=search_fn, gbp_fetch_fn=lambda *_: [])
    assert len(cands) == 1
    assert cands[0].candidate_gstin == _VALID_GSTIN
    assert cands[0].source == "web"
    assert cands[0].trade_name == "Sundaram Book Store"


_OTHER_GSTIN = "27ZZZZZ9999Z1Z3"  # a 2nd valid-format GSTIN for the noise fixtures


def test_fetch_candidates_web_drops_irrelevant_noise() -> None:
    # VT-448: a "<name> GST number" SERP for "RKeCom Services" returns Telecom GST pages that DON'T name
    # the business (they fuzzy-share only the generic "Services" token). The web leg must DROP them.
    def search_fn(_query: str) -> list[dict[str, Any]]:
        return [
            {"title": "Telecom Services", "description": f"GSTIN {_VALID_GSTIN}", "url": "x"},
            {"title": "Shubham Telecom Services", "description": f"GSTIN {_OTHER_GSTIN}", "url": "y"},
        ]

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=search_fn, gbp_fetch_fn=lambda *_: []
    )
    assert cands == []  # none echo the distinctive "rkecom" token → all dropped as noise


def test_fetch_candidates_web_keeps_named_match() -> None:
    # The owner's real business DOES name them in the result → kept (the filter is precision, not blanket).
    def search_fn(_query: str) -> list[dict[str, Any]]:
        return [{"title": "RKeCom Services Pvt Ltd", "description": f"GSTIN {_VALID_GSTIN}", "url": "x"}]

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=search_fn, gbp_fetch_fn=lambda *_: []
    )
    assert len(cands) == 1 and cands[0].candidate_gstin == _VALID_GSTIN


def test_fetch_candidates_web_all_generic_name_not_overfiltered() -> None:
    # An all-generic name ("Services Pvt Ltd") has no distinctive token → we must NOT over-filter.
    def search_fn(_query: str) -> list[dict[str, Any]]:
        return [{"title": "City Services GST", "description": f"GSTIN {_VALID_GSTIN}", "url": "x"}]

    cands = entity_match.fetch_candidates(
        "Services Pvt Ltd", "Mumbai", search_fn=search_fn, gbp_fetch_fn=lambda *_: []
    )
    assert len(cands) == 1  # sig_tokens empty → relevance gate passes everything


def test_business_name_matches_lenient_on_suffix_variation() -> None:
    # VT-448: the e2e case — typed "Pvt Ltd" vs registry "(OPC) PRIVATE LIMITED" still share 'rkecom'.
    assert entity_match.business_name_matches("RKeCom Services Pvt Ltd", "RKECOM SERVICES (OPC) PRIVATE LIMITED")
    assert entity_match.business_name_matches("Sundaram Book Store", "SUNDARAM BOOKS")


def test_business_name_matches_rejects_unrelated_valid_gstin() -> None:
    # The SECURITY case — a DIFFERENT business's (valid) GSTIN: no distinctive overlap → REJECT.
    assert not entity_match.business_name_matches("RKeCom Services Pvt Ltd", "SHUBHAM TELECOM SERVICES")
    assert not entity_match.business_name_matches("RKeCom Services Pvt Ltd", "AECOM INDIA PRIVATE LIMITED")


def test_business_name_matches_empty_registry_is_no_match() -> None:
    assert not entity_match.business_name_matches("RKeCom Services Pvt Ltd", None)
    assert not entity_match.business_name_matches("RKeCom Services Pvt Ltd", "")


def test_business_name_matches_all_generic_falls_back_to_substring() -> None:
    # An all-generic typed name (no distinctive token) → normalized substring/equality fallback.
    assert entity_match.business_name_matches("Services", "SERVICES INDIA PVT LTD")
    assert not entity_match.business_name_matches("Services", "TELECOM CORP")


def test_fetch_candidates_cin_registry_leg_surfaces_cin() -> None:
    # VT-449: a "<name> <city> CIN" SERP → a registry CIN candidate (the MCA Company-Master-Data input).
    def search_fn(q: str) -> list[dict[str, Any]]:
        if "CIN" in q:
            return [{"title": "RKeCom Services Pvt Ltd", "description": "CIN: U52609MH2020OPC344309", "url": "x"}]
        return []  # the GST-number leg finds nothing

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=search_fn, gbp_fetch_fn=lambda *_: []
    )
    assert "U52609MH2020OPC344309" in [c.candidate_cin for c in cands if c.source == "registry"]


def _gbp_one(*_: Any) -> list[dict[str, Any]]:
    return [{"title": "Sundaram Multi Pap Limited", "categoryName": "Stationery", "city": "Chennai"}]


def test_fetch_candidates_gbp_leg_has_no_gstin() -> None:
    cands = entity_match.fetch_candidates("Sundaram", "Chennai", search_fn=lambda _q: [], gbp_fetch_fn=_gbp_one)
    assert len(cands) == 1
    assert cands[0].source == "gbp"
    assert cands[0].candidate_gstin is None
    assert "Stationery" in (cands[0].detail or "")


def test_fetch_candidates_empty_name_returns_empty() -> None:
    assert entity_match.fetch_candidates("", "Chennai") == []


def test_fetch_candidates_web_failure_degrades_to_gbp() -> None:
    def boom(_q: str) -> list[dict[str, Any]]:
        raise RuntimeError("search down")

    def gbp(*_: Any) -> list[dict[str, Any]]:
        return [{"title": "Sundaram Books", "city": "Chennai"}]

    cands = entity_match.fetch_candidates("Sundaram", "Chennai", search_fn=boom, gbp_fetch_fn=gbp)
    assert [c.source for c in cands] == ["gbp"]  # web degraded, gbp survived


def test_confirm_rejects_malformed_gstin_without_calling_sandbox() -> None:
    called = {"n": 0}

    def lookup(_t: Any, _g: str) -> dict[str, Any]:
        called["n"] += 1
        return {"ok": True}

    out = entity_match.confirm_and_verify("t1", "NOTAGSTIN", lookup_fn=lookup)
    assert out["reason"] == "invalid_gstin_format"
    assert called["n"] == 0  # never hit the vendor on a malformed id


def test_confirm_verified_persists_anchor_and_seeds_discovery(monkeypatch) -> None:
    # Patch the two seams (NOT l1/verification — importing those pulls psycopg, absent in dep-less smoke).
    anchor: dict[str, Any] = {}
    seeded: dict[str, Any] = {}
    monkeypatch.setattr(
        entity_match, "_persist_anchor", lambda tid, **k: anchor.update(tid=tid, **k)
    )
    monkeypatch.setattr(entity_match, "_seed_discovery", lambda *a, **k: seeded.update(called=True))

    def lookup(tid: Any, g: str) -> dict[str, Any]:
        return {"ok": True, "status": "gstin_verified", "gstin": g, "name": "Sundaram Multi Pap Limited"}

    out = entity_match.confirm_and_verify("t1", _VALID_GSTIN, lookup_fn=lookup)
    assert out["status"] == "gstin_verified"
    # The verified path persists the anchor with the verified gstin + name, and seeds discovery.
    assert anchor["tid"] == "t1"
    assert anchor["gstin"] == _VALID_GSTIN
    assert anchor["verified_name"] == "Sundaram Multi Pap Limited"
    assert seeded.get("called") is True


def test_persist_anchor_builds_business_level_anchor() -> None:
    """_persist_anchor (injected upsert_fn — no l1 import) writes a business-level, sandbox-verified
    anchor onto the business_profile entity."""
    captured: dict[str, Any] = {}
    entity_match._persist_anchor(
        "t1", gstin=_VALID_GSTIN, verified_name="Sundaram Multi Pap Limited",
        upsert_fn=lambda tid, attrs: captured.update(tid=tid, attrs=attrs),
    )
    a = captured["attrs"]["business_entity_anchor"]
    assert a["gstin"] == _VALID_GSTIN
    assert a["source"] == "sandbox" and a["verified"] is True and a["registry_kind"] == "gst"
    assert a["trade_name"] == "Sundaram Multi Pap Limited"


def test_confirm_vendor_down_returns_unverified_no_anchor(monkeypatch) -> None:
    persisted = {"n": 0}
    monkeypatch.setattr(entity_match, "_persist_anchor", lambda *a, **k: persisted.update(n=persisted["n"] + 1))
    out = entity_match.confirm_and_verify(
        "t1", _VALID_GSTIN, lookup_fn=lambda _t, _g: {"ok": False, "reason": "vendor_down", "status": "unverified"}
    )
    assert out["reason"] == "vendor_down"
    assert persisted["n"] == 0  # no anchor on a non-verified result


def test_confirm_and_verify_empty_tenant_is_tenantless_no_db(monkeypatch) -> None:
    """Live-e2e regression (2026-06-28): the pre-create manual-GSTIN confirm passes tenant_id='' →
    run_lookup's tenant_connection('') 500'd. confirm_and_verify must take the TENANT-LESS path (Sandbox
    search only, NO _persist_anchor / DB) on an empty tenant_id."""
    from orchestrator.integrations.methods import sandbox_kyc

    persisted = {"n": 0}
    monkeypatch.setattr(entity_match, "_persist_anchor", lambda *a, **k: persisted.update(n=persisted["n"] + 1))
    monkeypatch.setattr(
        sandbox_kyc, "search_gstin",
        lambda g: sandbox_kyc.GstinLookup(ok=True, legal_name="RKECOM SERVICES OPC PRIVATE LIMITED", status="Active"),
    )
    out = entity_match.confirm_and_verify("", _VALID_GSTIN)  # empty tenant → tenantless, no 500
    assert out["ok"] and out["status"] == "gstin_verified" and out["name"]
    assert persisted["n"] == 0  # no anchor pre-create (no tenant to scope)


def test_verify_gstin_tenantless_vendor_down_fail_closed() -> None:
    # A vendor failure pre-create → vendor_down HOLD (never a false verify).
    from orchestrator.integrations.methods.sandbox_kyc import GstinLookup

    out = entity_match._verify_gstin_tenantless(_VALID_GSTIN, search_fn=lambda _g: GstinLookup(ok=False))
    assert out["ok"] is False and out["reason"] == "vendor_down"


def test_vt455_generic_biz_token_not_distinctive() -> None:
    """VT-455: short business-filler ('biz', 'ventures', 'global', 'mart', …) must NOT count as a
    distinctive token — a gibberish name sharing only 'biz' must NOT match unrelated '…biz…' rows."""
    assert "biz" not in entity_match._significant_tokens("zxqwvk nonexistent biz 99812")
    assert not entity_match._result_is_relevant(
        "MAXBIZ-CONNECT PRIVATE LIMITED", entity_match._significant_tokens("zxqwvk nonexistent biz 99812")
    )
    # a real distinctive name is unaffected
    assert "rkecom" in entity_match._significant_tokens("RKeCom Services Pvt Ltd")
