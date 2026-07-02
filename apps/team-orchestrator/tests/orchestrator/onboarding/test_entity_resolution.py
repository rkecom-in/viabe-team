"""VT-568 — unit tests for entity resolution (the RKeCom "wrong company" adjudication).

Pure logic, no network / no DB / no LLM key: the LLM adjudicator is INJECTED (``adjudicate_fn``) so
the DETERMINISTIC FLOOR runs for real against every case — a wrong LLM pick can be blocked by the
floor, and a correct one accepted. Dep-less: ``entity_resolution`` imports only stdlib at load;
``entity_match``/``knowyourgst_match`` (the name normalizer the floor reuses) are stdlib-only.
"""

from __future__ import annotations

from orchestrator.onboarding import entity_resolution as er
from orchestrator.onboarding.entity_resolution import GbpCandidate, OwnerAnchors, resolve_entity


def _accept(idx: int = 0, website: str | None = None, confidence: str = "high"):
    def _fn(anchors, candidates):
        return {
            "matched_candidate_index": idx, "resolved_website": website,
            "confidence": confidence, "reasoning": "picked candidate",
        }
    return _fn


def _reject(website: str | None = None):
    def _fn(anchors, candidates):
        return {
            "matched_candidate_index": None, "resolved_website": website,
            "confidence": "high", "reasoning": "none match",
        }
    return _fn


# --------------------------------------------------------------------------- the floor is a hard gate


def test_floor_blocks_phonetic_near_miss_even_when_llm_accepts():
    """The RKeCom case: GBP's 'Reecomps' shares no distinctive token with 'RKECOM' — the deterministic
    floor REJECTS it even though the (wrong) LLM verdict accepts idx 0. No GBP field can be trusted."""
    anchors = OwnerAnchors(
        signup_name="RKECOM",
        gst_legal_name="RKECOM SERVICES (OPC) PRIVATE LIMITED",
        gst_trade_name="RKECOM",
        gst_principal_address="A/403, Santacruz West, Mumbai, Maharashtra, 400054",
    )
    reecomps = GbpCandidate(
        index=0, title="Reecomps teleservices pvt ltd",
        category="Telecommunications service provider", city="Mumbai", website="https://reecomps.in",
    )
    res = resolve_entity(anchors, [reecomps], adjudicate_fn=_accept(idx=0))
    assert res.decision == "reject"
    assert res.matched_index is None
    assert "Reecomps teleservices pvt ltd" in res.rejected_titles


def test_locality_mismatch_rejects_even_on_name_match():
    """Same distinctive name but a clearly-different city (GST says Mumbai, candidate is Delhi) → the
    locality gate rejects. (Same-city ≠ same company, but a different city is a strong reject.)"""
    anchors = OwnerAnchors(signup_name="Sharma Sweets", gst_principal_address="MG Road, Mumbai, Maharashtra")
    cand = GbpCandidate(index=0, title="Sharma Sweets", city="Delhi", address="Connaught Place, Delhi")
    res = resolve_entity(anchors, [cand], adjudicate_fn=_accept(idx=0))
    assert res.decision == "reject"


# --------------------------------------------------------------------------- accept path


def test_correct_match_accepts_and_chains_website():
    anchors = OwnerAnchors(signup_name="Sharma Sweets")
    cand = GbpCandidate(index=0, title="Sharma Sweets", website="https://sharmasweets.example")
    res = resolve_entity(anchors, [cand], adjudicate_fn=_accept(idx=0))
    assert res.decision == "accept"
    assert res.matched_index == 0
    assert res.resolved_website == "https://sharmasweets.example"
    assert res.confidence == "high"


def test_accept_picks_the_right_candidate_among_several():
    """The near-miss ranks first but the real listing is second — the LLM picks 1 and the floor passes
    it while the floor-failing near-miss at 0 would never be accepted."""
    anchors = OwnerAnchors(signup_name="RKECOM", gst_trade_name="RKECOM")
    cands = [
        GbpCandidate(index=0, title="Reecomps teleservices pvt ltd", website="https://reecomps.in"),
        GbpCandidate(index=1, title="RKECOM Services", website="https://rkecom.in"),
    ]
    res = resolve_entity(anchors, cands, adjudicate_fn=_accept(idx=1))
    assert res.decision == "accept"
    assert res.matched_index == 1
    assert res.resolved_website == "https://rkecom.in"
    assert "Reecomps teleservices pvt ltd" in res.rejected_titles


def test_low_confidence_never_accepts():
    anchors = OwnerAnchors(signup_name="Sharma Sweets")
    cand = GbpCandidate(index=0, title="Sharma Sweets", website="https://sharmasweets.example")
    res = resolve_entity(anchors, [cand], adjudicate_fn=_accept(idx=0, confidence="low"))
    assert res.decision == "reject"


# --------------------------------------------------------------------------- fail-closed


def test_adjudicator_error_fails_closed_to_reject():
    def boom(anchors, candidates):
        raise RuntimeError("LLM unavailable")

    anchors = OwnerAnchors(signup_name="Sharma Sweets")
    cand = GbpCandidate(index=0, title="Sharma Sweets", website="https://sharmasweets.example")
    res = resolve_entity(anchors, [cand], adjudicate_fn=boom)
    assert res.decision == "reject"
    assert res.matched_index is None


def test_none_verdict_fails_closed_to_reject():
    anchors = OwnerAnchors(signup_name="Sharma Sweets")
    cand = GbpCandidate(index=0, title="Sharma Sweets")
    res = resolve_entity(anchors, [cand], adjudicate_fn=lambda a, c: None)
    assert res.decision == "reject"


def test_no_candidates_rejects():
    res = resolve_entity(OwnerAnchors(signup_name="X"), [], adjudicate_fn=_accept())
    assert res.decision == "reject"


# --------------------------------------------------------------------------- organic-resolved website


def test_reject_surfaces_plausible_organic_owner_website():
    anchors = OwnerAnchors(signup_name="RKECOM SERVICES", gst_trade_name="RKECOM")
    reecomps = GbpCandidate(index=0, title="Reecomps teleservices", website="https://reecomps.in")
    res = resolve_entity(anchors, [reecomps], adjudicate_fn=_reject(website="https://rkecom.in"))
    assert res.decision == "reject"
    # rkecom.in's domain label 'rkecom' plausibly matches the owner anchors → surfaced for the website source
    assert res.resolved_website == "https://rkecom.in"


def test_reject_drops_implausible_organic_website():
    """The LLM cannot inject an arbitrary/wrong domain: a resolved website whose domain doesn't match
    the owner name anchors is dropped (e.g. a hallucinated reecomps.in against {rkecom})."""
    anchors = OwnerAnchors(signup_name="RKECOM SERVICES", gst_trade_name="RKECOM")
    reecomps = GbpCandidate(index=0, title="Reecomps teleservices", website="https://reecomps.in")
    res = resolve_entity(anchors, [reecomps], adjudicate_fn=_reject(website="https://reecomps.in"))
    assert res.decision == "reject"
    assert res.resolved_website is None


# --------------------------------------------------------------------------- helpers


def test_domain_label_extraction():
    assert er._domain_label("https://www.rkecom.in/shop") == "rkecom"
    assert er._domain_label("rkecom.in") == "rkecom"
    assert er._domain_label("https://maps.google.com/place/1") is None  # a maps listing, not a domain


def test_website_plausible_matches_owner_name():
    assert er._website_plausible("https://rkecom.in", ["RKECOM SERVICES"]) is True
    assert er._website_plausible("https://reecomps.in", ["RKECOM"]) is False
    assert er._website_plausible(None, ["RKECOM"]) is False


def test_parse_verdict_tolerates_preamble_and_rejects_non_json():
    assert er._parse_verdict("") is None
    assert er._parse_verdict("I could not find a match.") is None
    assert er._parse_verdict('{"matched_candidate_index": 0, "confidence": "high"}')["matched_candidate_index"] == 0
    parsed = er._parse_verdict('Here is the result: {"matched_candidate_index": null, "confidence": "low"}')
    assert parsed["confidence"] == "low"
