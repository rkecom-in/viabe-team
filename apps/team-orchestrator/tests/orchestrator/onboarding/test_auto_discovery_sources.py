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
from orchestrator.onboarding.auto_discovery_sources import (
    SourceResult,
    discover_gbp,
    discover_serper,
    discover_website,
)

TENANT = uuid.uuid4()


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


def test_discover_gbp_ok_maps_fields_and_writes_draft(draft_spy, monkeypatch):
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
    result = discover_gbp(TENANT, seed, fetch_fn=fake_fetch)

    assert isinstance(result, SourceResult)
    assert result.source == "gbp"
    assert result.status == "ok"
    assert result.cost_usd == src._GBP_COST_USD
    # website preferred over url, surfaced for the GBP→website chain
    assert result.website == "https://sharmasweets.example"
    assert result.fields == {
        "business_name": "Sharma Sweets",
        "category": "Sweet shop",
        "city": "Jaipur",
        "rating": 4.6,
        "website": "https://sharmasweets.example",
    }
    # the seed name+city built the search query, token threaded through
    assert captured["token"] == "tok-test"
    assert captured["run_input"]["searchStringsArray"] == ["Sharma Sweets Jaipur"]
    assert captured["run_input"]["maxReviews"] == 0

    assert len(draft_spy) == 1
    assert draft_spy[0]["source"] == "gbp"
    assert draft_spy[0]["tenant_id"] == TENANT
    assert draft_spy[0]["fields"] == result.fields


def test_discover_gbp_website_falls_back_to_url(draft_spy, monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-test")
    place = {"title": "No-Site Cafe", "url": "https://maps.google/place/999"}
    result = discover_gbp(TENANT, {"business_name": "No-Site Cafe"}, fetch_fn=lambda ri, t: [place])
    assert result.status == "ok"
    assert result.website == "https://maps.google/place/999"
    assert result.fields["website"] == "https://maps.google/place/999"


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
