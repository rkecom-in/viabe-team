"""VT-366 Gap-2a — unit tests for the Auto-Discovery source adapters.

Behavioral, no network, no DB: ``write_draft`` is monkeypatched to a spy so the source
adapters never touch psycopg/Supabase. Each ``discover_*`` is exercised through its injected
``fetch_fn``/``extract_fn`` seams (the dep-injection the production code exposes for exactly
this), plus the env-gated skip paths.

``pytest.importorskip("psycopg")`` guards the dep-less smoke: ``auto_discovery_sources`` imports
``write_draft`` from ``draft_profile`` which imports ``orchestrator.db`` → psycopg at module top.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("psycopg")

from orchestrator.onboarding import auto_discovery_sources as src
from orchestrator.onboarding import entity_resolution as er
from orchestrator.onboarding.auto_discovery_sources import (
    SourceResult,
    discover_gbp,
    discover_gst,
    discover_serper,
    discover_website,
)

TENANT = uuid.uuid4()

# VT-568 — injected adjudicators (the LLM seam). The deterministic floor still runs for real; only the
# LLM verdict is stubbed — so a wrong LLM pick can be blocked by the floor, and a correct one accepted.


def _accept(idx: int = 0, website: str | None = None, confidence: str = "high"):
    def _fn(anchors, candidates):
        return {
            "matched_candidate_index": idx, "resolved_website": website,
            "confidence": confidence, "reasoning": "test-accept",
        }
    return _fn


def _reject(website: str | None = None):
    def _fn(anchors, candidates):
        return {
            "matched_candidate_index": None, "resolved_website": website,
            "confidence": "high", "reasoning": "test-reject",
        }
    return _fn


@pytest.fixture
def draft_spy(monkeypatch):
    """Replace the module-level ``write_draft`` with a recording spy (list of call kwargs).

    The source adapters call ``write_draft(tenant_id, fields, source=...)`` — recording every
    call lets each test assert the persistence side-effect without a DB.
    """
    calls: list[dict] = []

    def _spy(tenant_id, fields, *, source):
        calls.append({"tenant_id": tenant_id, "fields": fields, "source": source})

    monkeypatch.setattr(src, "write_draft", _spy)
    return calls


# --------------------------------------------------------------------------- GBP


def test_discover_gbp_accept_maps_fields_and_writes_draft(draft_spy, monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    place = {
        "title": "Sharma Sweets",
        "categoryName": "Sweet shop",
        "city": "Jaipur",
        "totalScore": 4.6,
        "website": "https://sharmasweets.example",
        "url": "https://maps.google/place/123",
    }
    captured: dict = {}

    def fake_fetch(run_input, token):
        captured["run_input"] = run_input
        captured["token"] = token
        return [place]

    seed = {"business_name": "Sharma Sweets", "city": "Jaipur"}
    # The floor passes ("Sharma Sweets" == "Sharma Sweets") and the LLM accepts candidate 0.
    result = discover_gbp(TENANT, seed, fetch_fn=fake_fetch, adjudicate_fn=_accept(idx=0))

    assert isinstance(result, SourceResult)
    assert result.source == "gbp"
    assert result.status == "ok"
    # cost = the GBP fetch + the entity-resolution adjudication (rides inside the GBP source)
    assert result.cost_usd == src._GBP_COST_USD + er.ADJUDICATION_COST_USD
    # the accepted candidate's website, surfaced for the GBP→website chain
    assert result.website == "https://sharmasweets.example"
    assert result.fields == {
        "business_name": "Sharma Sweets",
        "category": "Sweet shop",
        "city": "Jaipur",
        "rating": 4.6,
        "website": "https://sharmasweets.example",
        # VT-475: the GBP category 'Sweet shop' + name 'Sharma Sweets' reconcile to the 'sweets'
        # taxonomy key, written alongside the raw category (which is kept for transparency).
        "business_type": "sweets",
    }
    # the seed name+city built the search query; VT-568 bounds the fetch to top-N candidates
    assert captured["token"] == "tok-test"
    assert captured["run_input"]["searchStringsArray"] == ["Sharma Sweets Jaipur"]
    assert captured["run_input"]["maxReviews"] == 0
    assert captured["run_input"]["maxCrawledPlacesPerSearch"] == src._GBP_MAX_CANDIDATES

    # two writes: the entity_resolution decision provenance FIRST, then the accepted GBP fields.
    assert [c["source"] for c in draft_spy] == ["entity_resolution", "gbp"]
    assert draft_spy[1]["fields"] == result.fields
    prov = draft_spy[0]["fields"]["entity_resolution"]
    assert prov["decision"] == "accept"
    assert prov["matched_index"] == 0


def test_discover_gbp_accept_website_falls_back_to_url(draft_spy, monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    place = {"title": "No-Site Cafe", "url": "https://maps.google/place/999"}
    result = discover_gbp(
        TENANT, {"business_name": "No-Site Cafe"}, fetch_fn=lambda ri, t: [place], adjudicate_fn=_accept()
    )
    assert result.status == "ok"
    assert result.website == "https://maps.google/place/999"
    assert result.fields["website"] == "https://maps.google/place/999"


def test_discover_gbp_reject_writes_no_gbp_fields(draft_spy, monkeypatch):
    """VT-568 — the RKeCom case. GBP returns the phonetic near-miss 'Reecomps'; even when the LLM
    (wrongly) picks it, the deterministic name floor blocks it → NO GBP-derived field enters the draft
    (no telecom category, no reecomps.in, no wrong about). Only the audit provenance is recorded."""
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    place = {
        "title": "Reecomps teleservices pvt ltd",
        "categoryName": "Telecommunications service provider",
        "website": "https://reecomps.in",
        "city": "Mumbai",
    }
    seed = {
        "business_name": "RKECOM",
        "gst_legal_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "gst_trade_name": "RKECOM",
        "gst_principal_address": "A/403, Santacruz West, Mumbai, Maharashtra, 400054",
    }
    # LLM WRONGLY accepts idx 0 — the floor must still reject ("RKECOM" shares no token with "Reecomps").
    result = discover_gbp(TENANT, seed, fetch_fn=lambda ri, t: [place], adjudicate_fn=_accept(idx=0))

    assert result.status == "rejected"
    assert result.fields == {}
    assert result.website is None
    assert result.cost_usd == src._GBP_COST_USD + er.ADJUDICATION_COST_USD
    # ONLY the entity_resolution provenance was written — no GBP fields.
    assert [c["source"] for c in draft_spy] == ["entity_resolution"]
    prov = draft_spy[0]["fields"]["entity_resolution"]
    assert prov["decision"] == "reject"
    assert "Reecomps teleservices pvt ltd" in prov["rejected"]


def test_discover_gbp_reject_surfaces_organic_owner_website(draft_spy, monkeypatch):
    """On reject, an organic-resolved OWNER website (domain plausibly matches the name anchors) is still
    chained to the website source — the drill's ideal outcome: drop Reecomps, still discover rkecom.in."""
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    place = {"title": "Reecomps teleservices pvt ltd", "website": "https://reecomps.in", "city": "Mumbai"}
    seed = {"business_name": "RKECOM SERVICES", "gst_trade_name": "RKECOM"}
    result = discover_gbp(
        TENANT, seed, fetch_fn=lambda ri, t: [place], adjudicate_fn=_reject(website="https://rkecom.in")
    )
    assert result.status == "rejected"
    assert result.fields == {}
    # rkecom.in's domain label 'rkecom' matches the owner anchors → plausible → chained
    assert result.website == "https://rkecom.in"
    assert [c["source"] for c in draft_spy] == ["entity_resolution"]


def test_discover_gbp_no_token_skips_without_write(draft_spy, monkeypatch):
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)

    def _boom(run_input, token):  # must never be called when skipped
        raise AssertionError("fetch_fn should not run when token is absent")

    result = discover_gbp(
        TENANT, {"business_name": "Sharma Sweets", "city": "Jaipur"}, fetch_fn=_boom
    )
    assert result.status == "skipped"
    assert result.source == "gbp"
    assert result.cost_usd == 0.0
    assert result.fields == {}
    assert result.website is None
    assert draft_spy == []


def test_discover_gbp_no_query_skips(draft_spy, monkeypatch):
    """Seed lacking business_name → no query → skipped (token present but unusable)."""
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    result = discover_gbp(TENANT, {"city": "Jaipur"}, fetch_fn=lambda ri, t: [{"title": "x"}])
    assert result.status == "skipped"
    assert draft_spy == []


def test_discover_gbp_empty_items_is_empty_not_ok(draft_spy, monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    result = discover_gbp(TENANT, {"business_name": "Ghost Biz"}, fetch_fn=lambda ri, t: [])
    assert result.status == "empty"
    assert result.cost_usd == src._GBP_COST_USD  # the fetch was paid for
    assert draft_spy == []


def test_discover_gbp_explicit_token_overrides_env(draft_spy, monkeypatch):
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
    seen: dict = {}
    discover_gbp(
        TENANT,
        {"business_name": "Sharma Sweets"},
        token="explicit-tok",
        fetch_fn=lambda ri, t: seen.setdefault("tok", t) and [] or [],
    )
    assert seen["tok"] == "explicit-tok"


# ----------------------------------------------------------------------- website


def test_discover_website_ok_extracts_and_writes(draft_spy):
    fetched: dict = {}

    def fake_fetch(url):
        fetched["url"] = url
        return "  About us: we sell sweets.  "

    def fake_extract(text):
        fetched["text"] = text
        return {"about": "We sell sweets.", "services": ["sweets", "snacks"]}

    seed = {"website": "https://sharmasweets.example"}
    result = discover_website(TENANT, seed, fetch_fn=fake_fetch, extract_fn=fake_extract)

    assert result.source == "website"
    assert result.status == "ok"
    assert result.cost_usd == src._WEBSITE_COST_USD
    assert result.fields == {"about": "We sell sweets.", "services": ["sweets", "snacks"]}
    assert fetched["url"] == "https://sharmasweets.example"
    # the extractor gets the (capped) page text
    assert "About us" in fetched["text"]

    assert len(draft_spy) == 1
    assert draft_spy[0]["source"] == "website"
    assert draft_spy[0]["fields"] == result.fields


def test_discover_website_explicit_url_overrides_seed(draft_spy):
    seen: dict = {}

    def fake_fetch(url):
        seen["url"] = url
        return "text"

    discover_website(
        TENANT,
        {"website": "https://seed.example"},
        url="https://explicit.example",
        fetch_fn=fake_fetch,
        extract_fn=lambda t: {"about": "x"},
    )
    assert seen["url"] == "https://explicit.example"


def test_discover_website_no_url_skips(draft_spy):
    result = discover_website(TENANT, {}, fetch_fn=lambda u: "should not run")
    assert result.status == "skipped"
    assert result.source == "website"
    assert result.fields == {}
    assert draft_spy == []


def test_discover_website_fetch_raises_returns_error(draft_spy):
    def boom(url):
        raise RuntimeError("connection reset")

    result = discover_website(
        TENANT,
        {"website": "https://x.example"},
        fetch_fn=boom,
        extract_fn=lambda t: {"about": "never"},
    )
    assert result.status == "error"
    assert result.source == "website"
    assert result.cost_usd == 0.0
    assert draft_spy == []


def test_discover_website_blank_page_is_empty(draft_spy):
    result = discover_website(
        TENANT,
        {"website": "https://x.example"},
        fetch_fn=lambda u: "   ",
        extract_fn=lambda t: {"about": "should not be reached"},
    )
    assert result.status == "empty"
    assert draft_spy == []


def test_discover_website_extract_returns_nothing_is_empty(draft_spy):
    """Fetch succeeds but the extractor finds no usable fields → empty, no write."""
    result = discover_website(
        TENANT,
        {"website": "https://x.example"},
        fetch_fn=lambda u: "real page text",
        extract_fn=lambda t: {"about": None, "services": []},  # all falsy → filtered out
    )
    assert result.status == "empty"
    assert result.fields == {}
    assert draft_spy == []


# ------------------------------------------------------------------------ serper


def test_discover_serper_no_key_skips_never_errors(draft_spy, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    result = discover_serper(TENANT, {"business_name": "Sharma Sweets"})
    assert result.status == "skipped"
    assert result.source == "serper"
    assert result.cost_usd == 0.0
    assert draft_spy == []


# --------------------------------------------------------------------------- GST (VT-407)
#
# Driven through the REAL GstinLookup dataclass via the injected ``search_fn`` — so the PII guard
# exercises the production business_fields()/is_proprietorship() logic, not a stub.

from orchestrator.integrations.methods.sandbox_kyc import GstinLookup  # noqa: E402


def test_discover_gst_no_gstin_skips(draft_spy):
    """No gstin anchor in the seed → skipped (this source only runs once VT-406 sets the gstin)."""
    def _boom(gstin):  # must never be called when there's no gstin
        raise AssertionError("search_fn must not run without a gstin")

    result = discover_gst(TENANT, {"business_name": "X"}, search_fn=_boom)
    assert result.status == "skipped"
    assert result.source == "gst"
    assert result.cost_usd == 0.0
    assert result.fields == {}
    assert draft_spy == []


def test_discover_gst_active_company_writes_business_fields(draft_spy):
    """An ACTIVE company GSTIN → business-level fields written under source='gst'. For a company
    legal_name IS business-level (not a person) so it is included."""
    seen: dict = {}

    def fake_search(gstin):
        seen["gstin"] = gstin
        return GstinLookup(
            ok=True,
            legal_name="RKECOM SERVICES (OPC) PRIVATE LIMITED",
            trade_name="RKECOM",
            status="Active",
            constitution="Private Limited Company",
            registration_date="01/07/2017",
            nature_of_business=["Retail Business"],
            principal_address="12, MG Road, Mumbai, Maharashtra, 400001",
            additional_addresses=("7, Link Road, Pune, Maharashtra, 411001",),
            geo_lat="19.0760",
            geo_lng="72.8777",
        )

    result = discover_gst(TENANT, {"gstin": "27AAKCR3738B1ZE"}, search_fn=fake_search)

    assert seen["gstin"] == "27AAKCR3738B1ZE"
    assert result.status == "ok"
    assert result.source == "gst"
    assert result.cost_usd == src._GST_COST_USD == 0.0
    # company legal_name is business-level → present
    assert result.fields["legal_name"] == "RKECOM SERVICES (OPC) PRIVATE LIMITED"
    assert result.fields["trade_name"] == "RKECOM"
    assert result.fields["constitution"] == "Private Limited Company"
    assert result.fields["principal_address"].startswith("12, MG Road")
    assert result.fields["nature_of_business"] == ["Retail Business"]
    assert result.fields["additional_addresses"] == ["7, Link Road, Pune, Maharashtra, 411001"]
    assert result.fields["registration_date"] == "01/07/2017"

    assert len(draft_spy) == 1
    assert draft_spy[0]["source"] == "gst"
    assert draft_spy[0]["tenant_id"] == TENANT
    assert draft_spy[0]["fields"] == result.fields

    # VT-568 — the GST-verified identity anchors are surfaced into the seed for GBP adjudication.
    assert result.seed_updates == {
        "gst_legal_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "gst_trade_name": "RKECOM",
        "gst_principal_address": result.fields["principal_address"],
    }


def test_discover_gst_proprietorship_does_not_write_person_name(draft_spy):
    """PII NEGATIVE TEST (CL-390/425): for a Proprietorship, lgnm='Ramesh Kumar' is a natural
    person — it MUST NOT be written to the draft. Business-level fields still flow."""
    def fake_search(gstin):
        return GstinLookup(
            ok=True,
            legal_name="Ramesh Kumar",  # a PERSON for a proprietorship
            trade_name="Ramesh General Store",
            status="Active",
            constitution="Proprietorship",
            principal_address="5, Bazaar Road, Jaipur, Rajasthan, 302001",
        )

    result = discover_gst(TENANT, {"gstin": "08AAAAA0000A1Z5"}, search_fn=fake_search)

    assert result.status == "ok"
    # the person-name legal_name is NOT in the written fields
    assert "legal_name" not in result.fields
    assert "Ramesh Kumar" not in result.fields.values()
    # business-level context still written
    assert result.fields["trade_name"] == "Ramesh General Store"
    assert result.fields["constitution"] == "Proprietorship"
    assert result.fields["principal_address"].startswith("5, Bazaar Road")

    assert len(draft_spy) == 1
    assert "legal_name" not in draft_spy[0]["fields"]
    assert "Ramesh Kumar" not in draft_spy[0]["fields"].values()

    # VT-568 — the proprietor's personal name is NEVER surfaced as an anchor (gst_legal_name absent);
    # business-level anchors (trade name, locality) still cross.
    assert "gst_legal_name" not in result.seed_updates
    assert result.seed_updates["gst_trade_name"] == "Ramesh General Store"
    assert "Ramesh Kumar" not in result.seed_updates.values()


def test_discover_gst_vendor_down_errors_no_write(draft_spy):
    """search_gstin fail-closed (ok=False) → status 'error', nothing written."""
    result = discover_gst(
        TENANT, {"gstin": "27AAKCR3738B1ZE"}, search_fn=lambda g: GstinLookup(ok=False)
    )
    assert result.status == "error"
    assert result.source == "gst"
    assert result.cost_usd == 0.0
    assert draft_spy == []


def test_discover_gst_inactive_gstin_errors_no_write(draft_spy):
    """A parsed-but-inactive GSTIN (ok=True, status='Cancelled') is not useful context → error,
    no write (matches the verification semantics — only an active record earns trust)."""
    result = discover_gst(
        TENANT,
        {"gstin": "27AAKCR3738B1ZE"},
        search_fn=lambda g: GstinLookup(ok=True, legal_name="Dead Co", status="Cancelled"),
    )
    assert result.status == "error"
    assert draft_spy == []
