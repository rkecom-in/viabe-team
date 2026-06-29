"""VT-495 — KnowYourGSTScraper unit tests (injected fetch_fn / captured HTML; no network/creds).

Pins the reverse-engineered result-row parser + the fail-soft contract (no key / fetch error /
parse error / 0 results → []) + the in-process cache."""

from __future__ import annotations

import pytest

# Stdlib-only module, but its package __init__ chain pulls pydantic (absent in the dep-less smoke).
pytest.importorskip("pydantic")

from orchestrator.integrations.methods import knowyourgst as k  # noqa: E402

_RKECOM_GSTIN = "27AAKCR3738B1ZE"

# Faithful capture of the live knowyourgst.com by-name result markup (single result).
_RESULT_HTML = """
<div class="row"><div class="col l8 rightbox"><div class="row">
  <div class="col l12 s12 rightbox z-depth-1" id="searchresult">
    <a href="/gst-number-search/rkecom-services-opc-private-limited-27AAKCR3738B1ZE/"
       title="GST number of RKECOM SERVICES (OPC) PRIVATE LIMITED" target="blank">
      <h5>RKECOM SERVICES (OPC) PRIVATE LIMITED</h5>
    </a>
    <span class="black-text">
      <strong class="center-align">Maharashtra</strong>, <strong class="center-align">27AAKCR3738B1ZE</strong>
    </span>
  </div>
</div></div></div>
"""

# Two results in one page (different GSTINs) to exercise the multi-row + ordered-unique path.
_MULTI_HTML = _RESULT_HTML + """
<div class="col l12 s12 rightbox z-depth-1" id="searchresult">
  <a href="/gst-number-search/prodigy-digital-29AAACD1234A1Z5/" title="GST number of PRODIGY DIGITAL">
    <h5>PRODIGY DIGITAL</h5>
  </a>
  <span class="black-text"><strong>Karnataka</strong>, <strong>29AAACD1234A1Z5</strong></span>
</div>
"""


@pytest.fixture(autouse=True)
def _clear_state() -> None:
    # The cache + rate-window are module-global; reset between tests for determinism.
    k._cache.clear()
    k._calls.clear()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_single_result_row() -> None:
    rows = k._parse_results(_RESULT_HTML)
    assert rows == [{
        "company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "state": "Maharashtra",
        "gst_number": _RKECOM_GSTIN,
    }]


def test_parse_multiple_results_ordered_unique() -> None:
    rows = k._parse_results(_MULTI_HTML)
    assert [r["gst_number"] for r in rows] == [_RKECOM_GSTIN, "29AAACD1234A1Z5"]
    assert rows[1]["company_name"] == "PRODIGY DIGITAL" and rows[1]["state"] == "Karnataka"


def test_parse_gstin_falls_back_to_href_when_strong_missing_it() -> None:
    # If the meta <strong>s carry only the state, the GSTIN is recovered from the row href slug.
    html = """
    <a href="/gst-number-search/some-co-19ABCDE1234F1Z2/" title="GST number of SOME CO"><h5>SOME CO</h5></a>
    <span class="black-text"><strong>West Bengal</strong></span>
    """
    rows = k._parse_results(html)
    assert rows == [{"company_name": "SOME CO", "state": "West Bengal", "gst_number": "19ABCDE1234F1Z2"}]


def test_parse_empty_or_garbage_returns_empty() -> None:
    assert k._parse_results("") == []
    assert k._parse_results("<html><body>no results found</body></html>") == []


# ---------------------------------------------------------------------------
# search() — injected fetch_fn, fail-soft, cache
# ---------------------------------------------------------------------------

def test_search_parses_injected_html() -> None:
    scraper = k.KnowYourGSTScraper(fetch_fn=lambda _q: _RESULT_HTML)
    out = scraper.search("rkecom")
    assert out == [{
        "company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "state": "Maharashtra",
        "gst_number": _RKECOM_GSTIN,
    }]


def test_search_short_query_skips_fetch() -> None:
    calls = {"n": 0}

    def fetch(_q: str) -> str:
        calls["n"] += 1
        return _RESULT_HTML

    out = k.KnowYourGSTScraper(fetch_fn=fetch).search("rk")  # < 5 chars (site minlength)
    assert out == [] and calls["n"] == 0


def test_search_no_key_no_fetch_fn_fails_open(monkeypatch) -> None:
    monkeypatch.delenv("SCRAPINGBEE_API_KEY", raising=False)
    assert k.KnowYourGSTScraper().search("rkecom") == []


def test_search_fetch_error_degrades_to_empty() -> None:
    def boom(_q: str) -> str:
        raise RuntimeError("scrapingbee down")

    assert k.KnowYourGSTScraper(fetch_fn=boom).search("rkecom") == []  # never raises out


def test_search_parse_error_degrades_to_empty(monkeypatch) -> None:
    monkeypatch.setattr(k, "_parse_results", lambda _h: (_ for _ in ()).throw(ValueError("boom")))
    assert k.KnowYourGSTScraper(fetch_fn=lambda _q: _RESULT_HTML).search("rkecom") == []


def test_search_caches_successful_result() -> None:
    calls = {"n": 0}

    def fetch(_q: str) -> str:
        calls["n"] += 1
        return _RESULT_HTML

    scraper = k.KnowYourGSTScraper(fetch_fn=fetch)
    first = scraper.search("rkecom")
    second = scraper.search("RKECOM")  # same normalized key (case-insensitive) → cache hit
    assert first == second and calls["n"] == 1


def test_scraper_configured_reflects_key(monkeypatch) -> None:
    monkeypatch.delenv("SCRAPINGBEE_API_KEY", raising=False)
    assert k.scraper_configured() is False
    monkeypatch.setenv("SCRAPINGBEE_API_KEY", "x")
    assert k.scraper_configured() is True
