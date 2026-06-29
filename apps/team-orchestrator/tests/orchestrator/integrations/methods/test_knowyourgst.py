"""VT-495 — KnowYourGSTScraper unit tests (injected fetch_fn / captured HTML; no network/creds).

Pins the reverse-engineered result-row parser + the fail-soft contract (no key / fetch error /
parse error / 0 results → []) + the in-process cache + VT-509 empty-cache / retry fixes."""

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

# VT-509: exact markup captured from a live ScrapingBee run 2026-06-30 (3 consecutive runs).
# The anchor has extra whitespace / blank line before <h5> (real Django-rendered page), and
# <strong> tags carry class="center-align" — the parser must handle both.
_LIVE_RESULT_HTML = (
    '<div class="col l12 s12 rightbox z-depth-1" id="searchresult">'
    '\n    <a href="/gst-number-search/rkecom-services-opc-private-limited-27AAKCR3738B1ZE/"'
    ' title="GST number of RKECOM SERVICES (OPC) PRIVATE LIMITED" target="blank">'
    '\n        \n        <h5>RKECOM SERVICES (OPC) PRIVATE LIMITED</h5>'
    '\n        </a>'
    '\n          <span class="black-text">'
    '\n          <strong class="center-align">Maharashtra</strong>,'
    ' <strong class="center-align">27AAKCR3738B1ZE</strong>'
    '\n          </span>'
    '\n    </div>'
)

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


# ---------------------------------------------------------------------------
# VT-509 — live-format HTML fixture + empty-cache + bounded-retry tests
# ---------------------------------------------------------------------------

def test_parse_live_format_html_with_blank_line_and_center_align_class() -> None:
    """VT-509: pin the EXACT markup from three consecutive live ScrapingBee runs 2026-06-30.
    The anchor has a blank line before <h5> and <strong> carries class="center-align" — both
    must parse correctly (DEFECT 2 raw evidence)."""
    rows = k._parse_results(_LIVE_RESULT_HTML)
    assert rows == [{
        "company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "state": "Maharashtra",
        "gst_number": _RKECOM_GSTIN,
    }]


def test_empty_result_not_written_to_db_cache(monkeypatch) -> None:
    """VT-509 DEFECT 2 root-cause fix: a scrape returning [] (form missed / bot-block) must NOT
    be written to the L2 DB persistent cache. A stale [] in the DB was the root cause of "both
    cards source=web" in Fazal's live run — subsequent scrapes got cached [] for 24h."""
    db_put_calls: list = []
    monkeypatch.setattr(k, "_db_cache_put", lambda key, rows: db_put_calls.append((key, rows)))

    # Scraper returns empty HTML (no results)
    scraper = k.KnowYourGSTScraper(fetch_fn=lambda _q: "<html>no results found</html>")
    result = scraper.search("rkecom")
    assert result == []
    # CRITICAL: the empty result must NOT be persisted to the DB cache
    assert db_put_calls == [], f"Empty result was written to DB cache: {db_put_calls}"


def test_successful_result_is_written_to_db_cache(monkeypatch) -> None:
    """Complement: a non-empty scrape result MUST still be written to the L2 DB cache (VT-507)."""
    db_put_calls: list = []
    monkeypatch.setattr(k, "_db_cache_put", lambda key, rows: db_put_calls.append((key, rows)))

    scraper = k.KnowYourGSTScraper(fetch_fn=lambda _q: _RESULT_HTML)
    result = scraper.search("rkecom")
    assert result == [{"company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
                       "state": "Maharashtra", "gst_number": _RKECOM_GSTIN}]
    # Non-empty result MUST be persisted
    assert len(db_put_calls) == 1 and db_put_calls[0][1] == result


def test_bounded_retry_fires_on_empty_live_scrape(monkeypatch) -> None:
    """VT-509: when the first live ScrapingBee fetch returns empty HTML (form submit missed),
    the scraper retries once — and if the retry succeeds, returns the result."""
    # First call returns empty; second call returns the real HTML
    call_count = {"n": 0}
    real_html = _RESULT_HTML

    def fake_scrapingbee(self, query: str) -> str:  # noqa: ANN001
        call_count["n"] += 1
        return "<html>no results</html>" if call_count["n"] == 1 else real_html

    monkeypatch.setattr(k.KnowYourGSTScraper, "_scrapingbee_fetch", fake_scrapingbee)
    monkeypatch.setenv("SCRAPINGBEE_API_KEY", "fakekey")

    scraper = k.KnowYourGSTScraper()
    result = scraper.search("rkecom")
    assert call_count["n"] == 2  # retry fired
    assert result == [{"company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
                       "state": "Maharashtra", "gst_number": _RKECOM_GSTIN}]


def test_bounded_retry_does_not_fire_for_injected_fixture(monkeypatch) -> None:
    """Injected fetch_fn (test path) MUST NOT trigger the retry — retry is live-ScrapingBee only."""
    call_count = {"n": 0}

    def fixture(_q: str) -> str:
        call_count["n"] += 1
        return "<html>no results</html>"

    scraper = k.KnowYourGSTScraper(fetch_fn=fixture)
    result = scraper.search("rkecom")
    assert result == []
    assert call_count["n"] == 1  # exactly one call — no retry for injected fixtures
