"""VT-495 — KnowYourGST name→GSTIN scraper (ScrapingBee-rendered).

Implements the ``knowyourgst_match.GSTSearcher`` Protocol: ``search(query) -> list[{company_name,
state, gst_number}]`` over knowyourgst.com (a PUBLIC GST-records directory). This is the discovery
leg that surfaces GSTIN CANDIDATES from a business NAME *before* the owner is asked to type a GSTIN —
HINTs only; the Sandbox GST verify (``sandbox_kyc.search_gstin``) stays the SOLE authoritative gate.

Reverse-engineered 2026-06-30 (live RKECOM canary):
- The by-name search lives at ``/gst-number-search/by-name-pan/``. It is a Django **POST** form
  (text field ``gstnum``, minlength 5 / maxlength 50 — matching the matching-layer constants —
  guarded by a CSRF token). A plain GET only echoes the query back into the form's ``value`` and
  runs NO search, so we drive the real form through ScrapingBee's headless browser via a
  ``js_scenario`` (fill ``#gstnumber`` → click the "Search GST number" submit → wait for render).
  The browser handles the CSRF cookie/token automatically; ScrapingBee returns the rendered HTML.
- Result row markup:
    <a href="/gst-number-search/<slug>-<GSTIN>/" title="GST number of <NAME>" target="blank">
      <h5><NAME></h5>
    </a>
    <span class="black-text"><strong>STATE</strong>, <strong>GSTIN</strong></span>
  We parse with the stdlib (``re`` + ``html.unescape``) — the orchestrator ships NO HTML-parser
  dependency (bs4/lxml/selectolax), and ``auto_discovery_sources._fetch_website`` already sets the
  regex-strip precedent. ``_parse_results`` is a pure function (unit-tested off captured HTML).

FAIL-SOFT EVERYWHERE (best-effort discovery — must NEVER block onboarding): missing key, network /
ScrapingBee error, parse error, 0 results → ``[]``. ``search()`` never raises out. Cost guards: an
in-process TTL cache (repeat queries don't re-bill) + a sliding-window rate-limit circuit-breaker
(a runaway/abuse burst is skipped → ``[]``, fail-open). ScrapingBee credits + knowyourgst ToS.
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_KEY_ENV = "SCRAPINGBEE_API_KEY"
_SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"
_FORM_URL = "https://www.knowyourgst.com/gst-number-search/by-name-pan/"
# The search form's field + submit (reverse-engineered). The CSS selectors drive the headless fill.
_QUERY_FIELD_SELECTOR = "#gstnumber"  # <input name="gstnum" id="gstnumber" minlength=5 maxlength=50>
_SUBMIT_SELECTOR = 'input[value="Search GST number"]'
_RENDER_WAIT_MS = 6000  # let the POST submit + server-rendered results settle before capture
_TIMEOUT_S = 120.0  # render_js + js_scenario is slow; generous so a slow-but-eventual 200 isn't lost
_MIN_QUERY_LEN = 5  # the site rejects <5 chars (form minlength); skip the call entirely

# GSTIN: 2 state digits + PAN(5 letters + 4 digits + 1 letter) + 1 entity char + 'Z' + 1 checksum.
_GSTIN_RE = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]")
# One result unit: the result anchor (h5 name) immediately followed by its black-text meta span.
_RESULT_RE = re.compile(
    r'<a\b[^>]*href="/gst-number-search/[^"]*"[^>]*>\s*<h5[^>]*>(?P<name>.*?)</h5>\s*</a>'
    r'\s*<span\b[^>]*class="[^"]*black-text[^"]*"[^>]*>(?P<meta>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_STRONG_RE = re.compile(r"<strong\b[^>]*>(?P<v>.*?)</strong>", re.IGNORECASE | re.DOTALL)

# In-process cost guards (single-process orchestrator runtime).
_CACHE_TTL_S = 6 * 3600  # GST registry rows are stable; don't re-bill the same query for 6h
_RATE_MAX = 60  # at most N ScrapingBee calls per window (circuit-breaker, not the expected rate)
_RATE_WINDOW_S = 60.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_calls: deque[float] = deque()


def scraper_configured() -> bool:
    """True iff a ScrapingBee key is present — the leg's fail-open gate (no key → discovery skipped,
    onboarding falls through to the existing legs + manual-GSTIN path)."""
    return bool(os.environ.get(_KEY_ENV, "").strip())


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def _clean_text(s: str) -> str:
    return _html.unescape(re.sub(r"\s+", " ", _strip_tags(s))).strip()


def _parse_results(html_text: str) -> list[dict[str, str]]:
    """Parse the rendered search HTML into ``[{company_name, state, gst_number}]`` (ordered-unique by
    GSTIN). Pure + defensive: malformed / 0-result markup → ``[]``. Each result's meta span carries
    two <strong> cells — STATE then GSTIN; the GSTIN is matched by shape (fallback: the row href)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _RESULT_RE.finditer(html_text or ""):
        name = _clean_text(m.group("name"))
        strongs = [_clean_text(x.group("v")) for x in _STRONG_RE.finditer(m.group("meta"))]
        gstin = ""
        state = ""
        for cell in strongs:
            compact = cell.upper().replace(" ", "")
            if _GSTIN_RE.fullmatch(compact):
                gstin = compact
            elif not state:
                state = cell
        if not gstin:  # fallback: pull the GSTIN out of the row href slug
            href_match = _GSTIN_RE.search(m.group(0).upper())
            gstin = href_match.group(0) if href_match else ""
        if not name or not gstin or gstin in seen:
            continue
        seen.add(gstin)
        out.append({"company_name": name, "state": state, "gst_number": gstin})
    return out


def _cache_get(key: str) -> list[dict[str, str]] | None:
    with _lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        exp, rows = hit
        if time.time() >= exp:
            _cache.pop(key, None)
            return None
        return list(rows)


def _cache_put(key: str, rows: list[dict[str, str]]) -> None:
    with _lock:
        _cache[key] = (time.time() + _CACHE_TTL_S, list(rows))


def _rate_allow() -> bool:
    """Sliding-window circuit-breaker. Returns False (skip the call, fail-open) once the window is
    saturated — bounds ScrapingBee credit spend under a runaway loop / abuse burst."""
    now = time.time()
    with _lock:
        while _calls and now - _calls[0] > _RATE_WINDOW_S:
            _calls.popleft()
        if len(_calls) >= _RATE_MAX:
            return False
        _calls.append(now)
        return True


class KnowYourGSTScraper:
    """ScrapingBee-backed ``GSTSearcher``. ``fetch_fn`` is injectable for unit tests (no network /
    credits): ``(query) -> rendered_html``. With no ``fetch_fn`` and no key, ``search`` fail-opens
    to ``[]``."""

    def __init__(self, api_key: str | None = None, *, fetch_fn=None) -> None:
        self._api_key = (api_key or os.environ.get(_KEY_ENV, "")).strip()
        self._fetch_fn = fetch_fn

    def search(self, query: str) -> list[dict[str, str]]:
        """Run one by-name search → ``[{company_name, state, gst_number}]``. NEVER raises out."""
        query = (query or "").strip()
        if len(query) < _MIN_QUERY_LEN:
            return []  # the site rejects <5 chars — skip the (billed) call
        cache_key = query.lower()
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        if self._fetch_fn is None and not self._api_key:
            return []  # fail-open: no key → discovery skipped, caller falls through to manual
        if self._fetch_fn is None and not _rate_allow():
            logger.warning("knowyourgst: rate-limit window saturated — skipping search (fail-open)")
            return []
        try:
            html_text = (self._fetch_fn or self._scrapingbee_fetch)(query)
        except Exception:  # noqa: BLE001 — fragile network/vendor; degrade, never raise into signup
            logger.warning("knowyourgst: fetch failed for query (degrade to none)", exc_info=True)
            return []
        try:
            rows = _parse_results(html_text)
        except Exception:  # noqa: BLE001 — markup drift must degrade, not raise
            logger.warning("knowyourgst: parse failed (degrade to none)", exc_info=True)
            return []
        _cache_put(cache_key, rows)  # cache successful parses (incl. legit 0-results), not errors
        return rows

    def _scrapingbee_fetch(self, query: str) -> str:
        """Drive the Django by-name POST form through ScrapingBee's headless browser (fill + submit +
        wait), returning the rendered results HTML. Raises on transport/HTTP error → ``search`` degrades."""
        import httpx

        scenario = {
            "instructions": [
                {"fill": [_QUERY_FIELD_SELECTOR, query]},
                {"click": _SUBMIT_SELECTOR},
                {"wait": _RENDER_WAIT_MS},
            ]
        }
        params = {
            "api_key": self._api_key,
            "url": _FORM_URL,
            "render_js": "true",
            "js_scenario": json.dumps(scenario),
        }
        resp = httpx.get(_SCRAPINGBEE_URL, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.text
