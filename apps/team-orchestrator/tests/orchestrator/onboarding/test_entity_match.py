"""VT-406 — entity-match unit tests (injected fns; no network/creds/DB)."""

from __future__ import annotations

from typing import Any

import pytest

# entity_match transitively imports orchestrator.integrations → pydantic, which is absent
# in the lean CI ``test`` job / pre-push dep-less smoke. Skip there so collection never
# breaks (the full ``orchestrator`` job, deps present, runs these).
pytest.importorskip("pydantic")

from orchestrator.onboarding import entity_match  # noqa: E402

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


_LLM_CIN = "U52609MH2020OPC344309"
_LLM_COMPANY = "RKECOM SERVICES (OPC) PRIVATE LIMITED"


def _llm_json(companies: list[dict]) -> str:
    """Helper: build a JSON blob as the new structured LLM response format."""
    import json
    return json.dumps({"companies": companies})


def test_vt452_llm_leg_parses_gstin_cin_name_as_candidates() -> None:
    """VT-452/VT-509: the LLM leg now returns strict JSON {"companies":[{name,gstin,cin}]}.
    trade_name is the LLM-REPORTED registered name (NEVER the echoed query string).
    An injected llm_fn forces the leg on regardless of the flag."""
    def llm_fn(name: str, city: str) -> str:
        assert "RKeCom" in name and city == "Mumbai"
        return _llm_json([{"name": _LLM_COMPANY, "gstin": _VALID_GSTIN, "cin": _LLM_CIN}])

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn
    )
    llm = [c for c in cands if c.source == "llm"]
    assert _VALID_GSTIN in [c.candidate_gstin for c in llm]
    assert _LLM_CIN in [c.candidate_cin for c in llm]
    # VT-509: trade_name is the LLM-REPORTED registered name — NEVER the echoed query string.
    for c in llm:
        assert c.trade_name == _LLM_COMPANY  # registry name from JSON, not "RKeCom Services Pvt Ltd"
        assert not hasattr(c, "verified")  # EntityCandidate has no verified field — it is never "verified"


def test_vt452_llm_leg_degrades_to_empty_on_failure() -> None:
    # An LLM/network error in the leg → [] (fail-soft, like the other legs); never raises into signup.
    def boom(_n: str, _c: str) -> str:
        raise RuntimeError("anthropic down")

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=boom
    )
    assert cands == []  # web/gbp empty + llm degraded


def test_vt452_llm_leg_drops_irrelevant_answer() -> None:
    # The JSON company name doesn't contain a distinctive RKeCom token → relevance filter drops it.
    def llm_fn(_n: str, _c: str) -> str:
        return _llm_json([{"name": "Shubham Telecom Services", "gstin": _OTHER_GSTIN, "cin": ""}])

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn
    )
    assert [c for c in cands if c.source == "llm"] == []  # no 'rkecom' token → dropped as noise


def test_vt452_llm_leg_flag_gated_off_by_default(monkeypatch) -> None:
    # With NO injected llm_fn and the flag OFF (default), the leg is NOT called — _default_llm_search
    # never runs (so no real LLM/key needed in the unit env). Only web/gbp legs contribute.
    called = {"n": 0}
    monkeypatch.setattr(
        entity_match, "_default_llm_search", lambda n, c: called.__setitem__("n", called["n"] + 1) or ""
    )
    from orchestrator import feature_flags

    monkeypatch.setattr(feature_flags, "llm_discovery_enabled", lambda: False)
    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: []
    )
    assert called["n"] == 0  # flag off + no injected fn → leg skipped, default never invoked
    assert [c for c in cands if c.source == "llm"] == []


def test_vt452_llm_leg_flag_gated_on_calls_default(monkeypatch) -> None:
    # With the flag ON and no injected fn, the leg calls the default LLM search (here mocked as JSON).
    from orchestrator import feature_flags

    monkeypatch.setattr(feature_flags, "llm_discovery_enabled", lambda: True)
    monkeypatch.setattr(
        entity_match, "_default_llm_search",
        lambda n, c: _llm_json([{"name": "RKECOM SERVICES OPC PVT LTD", "gstin": _VALID_GSTIN, "cin": ""}]),
    )
    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: []
    )
    llm = [c for c in cands if c.source == "llm"]
    assert len(llm) == 1 and llm[0].candidate_gstin == _VALID_GSTIN


# ---------------------------------------------------------------------------
# VT-509 — LLM structured-output adversarial tests (DEFECT 1 regression suite)
# ---------------------------------------------------------------------------

def test_vt509_llm_freetext_notfound_returns_zero_candidates() -> None:
    """VT-509 DEFECT 1: an LLM 'not found' MONOLOGUE (free-text, not JSON) must produce ZERO
    candidates — it must NEVER be shown as a Found card with the query echoed as the name."""
    def llm_fn(_n: str, _c: str) -> str:
        return (
            "I'll search public records for RKeCom Service Pvt Ltd...\n"
            "## Result: Not Found\n"
            "I could not find a GSTIN for RKeCom Service Pvt Ltd in public records.\n"
            "No GST number found."
        )

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn
    )
    assert [c for c in cands if c.source == "llm"] == []  # free-text "not found" → zero candidates


def test_vt509_llm_json_empty_companies_returns_zero_candidates() -> None:
    """VT-509: {"companies": []} → zero candidates (LLM found nothing in structured form)."""
    def llm_fn(_n: str, _c: str) -> str:
        return _llm_json([])  # {"companies": []}

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn
    )
    assert [c for c in cands if c.source == "llm"] == []


def test_vt509_llm_json_uses_reported_name_not_query() -> None:
    """VT-509: trade_name on the LLM candidate MUST be the JSON-reported registered company name,
    NOT the queried/echoed input string (the original bug echoed the query as trade_name)."""
    def llm_fn(_n: str, _c: str) -> str:
        return _llm_json([{"name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "gstin": _VALID_GSTIN, "cin": ""}])

    cands = entity_match.fetch_candidates(
        "RKeCom Service Pvt Ltd",  # intentionally slightly different from the registry name
        "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn,
    )
    llm = [c for c in cands if c.source == "llm"]
    assert len(llm) == 1
    # trade_name must be the LLM-reported name, NOT "RKeCom Service Pvt Ltd" (the query)
    assert llm[0].trade_name == "RKECOM SERVICES (OPC) PRIVATE LIMITED"
    assert llm[0].candidate_gstin == _VALID_GSTIN


def test_vt509_llm_json_name_only_no_gstin_dropped() -> None:
    """VT-509: a JSON company entry with a name but NO valid 15-char GSTIN must be DROPPED —
    it must NOT produce a name-only candidate card with 'No GST number found.'"""
    def llm_fn(_n: str, _c: str) -> str:
        return _llm_json([
            {"name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "gstin": "", "cin": ""},  # no GSTIN
            {"name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "gstin": "NOTVALID", "cin": ""},  # bad shape
        ])

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn
    )
    # Neither entry has a valid GSTIN or CIN → zero LLM candidates (not a garbage "found" card)
    assert [c for c in cands if c.source == "llm"] == []


def test_vt509_llm_json_with_preamble_still_parsed() -> None:
    """VT-509 robustness: the web_search flow may prepend a short sentence before the JSON object.
    The parser must extract the embedded JSON rather than failing on the preamble."""
    def llm_fn(_n: str, _c: str) -> str:
        # Simulates a model that says one sentence then gives the JSON
        return f'Here is the result: {_llm_json([{"name": _LLM_COMPANY, "gstin": _VALID_GSTIN, "cin": ""}])}'

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], llm_fn=llm_fn
    )
    llm = [c for c in cands if c.source == "llm"]
    assert len(llm) == 1 and llm[0].candidate_gstin == _VALID_GSTIN


def test_vt455_generic_biz_token_not_distinctive() -> None:
    """VT-455: short business-filler ('biz', 'ventures', 'global', 'mart', …) must NOT count as a
    distinctive token — a gibberish name sharing only 'biz' must NOT match unrelated '…biz…' rows."""
    assert "biz" not in entity_match._significant_tokens("zxqwvk nonexistent biz 99812")
    assert not entity_match._result_is_relevant(
        "MAXBIZ-CONNECT PRIVATE LIMITED", entity_match._significant_tokens("zxqwvk nonexistent biz 99812")
    )
    # a real distinctive name is unaffected
    assert "rkecom" in entity_match._significant_tokens("RKeCom Services Pvt Ltd")


# ---------------------------------------------------------------------------
# VT-495 — knowyourgst.com name→GSTIN discovery leg (runs BEFORE the manual-GSTIN-entry step)
# ---------------------------------------------------------------------------

_RKECOM_GSTIN = "27AAKCR3738B1ZE"  # real RKECOM GSTIN (public record), valid 15-char shape


class _FakeKyg:
    """Injectable knowyourgst GSTSearcher fixture (the matching layer drives .search)."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def search(self, query: str) -> list[dict[str, str]]:
        self.queries.append(query)
        return self.rows


def test_vt495_knowyourgst_leg_surfaces_gstin_candidate() -> None:
    # The durable RKeCom fix: a name→GSTIN candidate surfaces FIRST, BEFORE the owner types a GSTIN.
    kyg = _FakeKyg(
        [{"company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "state": "Maharashtra", "gst_number": _RKECOM_GSTIN}]
    )
    cands = entity_match.fetch_candidates(
        "RKECOM Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: [], kyg_scraper=kyg
    )
    surfaced = [c for c in cands if c.source == "knowyourgst"]
    assert len(surfaced) == 1
    assert surfaced[0].candidate_gstin == _RKECOM_GSTIN
    assert surfaced[0].trade_name == "RKECOM SERVICES (OPC) PRIVATE LIMITED"
    assert "Maharashtra" in (surfaced[0].detail or "")
    # the matching layer issued the normalized distinctive query (services/pvt/ltd stripped)
    assert kyg.queries == ["rkecom"]
    # HINT-only invariant — the candidate is never marked verified (Sandbox verify is the gate)
    assert not hasattr(surfaced[0], "verified")


def test_vt495_knowyourgst_leg_fail_open_on_error_never_blocks() -> None:
    # The scrape erroring must NOT block onboarding: the leg degrades to [] and the OTHER legs
    # (here the web leg) + the manual-GSTIN path remain.
    class _Boom:
        def search(self, query: str) -> list[dict[str, str]]:
            raise RuntimeError("scrapingbee down")

    cands = entity_match.fetch_candidates(
        "RKeCom Services Pvt Ltd",
        "Mumbai",
        search_fn=lambda _q: [{"title": "RKeCom Services", "description": f"GSTIN {_VALID_GSTIN}", "url": "x"}],
        gbp_fetch_fn=lambda *_: [],
        kyg_scraper=_Boom(),
    )
    assert [c for c in cands if c.source == "knowyourgst"] == []  # leg degraded, no raise
    assert any(c.source == "web" for c in cands)  # onboarding continues on the surviving legs


def test_vt495_knowyourgst_leg_skipped_without_key(monkeypatch) -> None:
    # No injected scraper + no SCRAPINGBEE_API_KEY → the leg self-skips (fail-open to manual path).
    monkeypatch.delenv("SCRAPINGBEE_API_KEY", raising=False)
    cands = entity_match.fetch_candidates(
        "RKECOM Services Pvt Ltd", "Mumbai", search_fn=lambda _q: [], gbp_fetch_fn=lambda *_: []
    )
    assert [c for c in cands if c.source == "knowyourgst"] == []


def test_vt495_knowyourgst_leg_dedups_gstin_against_web_leg() -> None:
    # knowyourgst runs first; a later web leg surfacing the SAME GSTIN is deduped (one candidate).
    kyg = _FakeKyg(
        [{"company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "state": "Maharashtra", "gst_number": _RKECOM_GSTIN}]
    )
    cands = entity_match.fetch_candidates(
        "RKECOM Services Pvt Ltd",
        "Mumbai",
        search_fn=lambda _q: [{"title": "RKECOM Services", "description": f"GSTIN {_RKECOM_GSTIN}", "url": "x"}],
        gbp_fetch_fn=lambda *_: [],
        kyg_scraper=kyg,
    )
    matching = [c for c in cands if c.candidate_gstin == _RKECOM_GSTIN]
    assert len(matching) == 1 and matching[0].source == "knowyourgst"  # first-seen wins
