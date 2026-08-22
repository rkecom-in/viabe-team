"""VT-778 — DataForSEO Google **AI Mode** as a name→GSTIN discovery leg.

Why AI Mode and NOT organic search (measured 2026-08-21, `canaries/dataforseo_gstin_eval.py`,
scored against known truth rather than eyeballed):

    Balaji Wafers      AI Mode  -> 4 GSTINs, ALL Balaji's (1 exact + 3 same-PAN states), 0 strangers
                       organic  -> the right ones PLUS 3-4 other companies' registrations, every time
    RKeCom Services    AI Mode  -> NOTHING
                       organic  -> 10 GSTINs, none of them RKeCom's, none paired with the name

Precision is what decides this leg, not recall. A plausible GSTIN belonging to the WRONG company is
the VT-406 Sundaram failure, and the Sandbox gate cannot catch it — Sandbox confirms the number is
real and ACTIVE because it belongs to *somebody*, and cannot know the owner picked the wrong
somebody. Organic's noise is therefore disqualifying, not merely untidy.

AI Mode's PROSE CANNOT BE TRUSTED, and this was nearly missed. A first evaluation run returned no
GSTIN at all for RKeCom, which read as "it declines rather than invents". A second run returned
``27AAECR0564M1Z3`` — a well-formed Maharashtra GSTIN that is NOT RKeCom's (the real one is
27AAKCR3738B1ZE), emitted inside the sentence "If you need to verify or pull their exact 15-digit
GSTIN, you can...". An illustrative number in synthesised prose, indistinguishable from a real
answer once the surrounding words are stripped away. One sample had said "safe"; the second said
otherwise.

So extraction reads the ``references`` array ONLY — the sources AI Mode actually cited — and never
the prose. Measured on the same two businesses:

    Balaji Wafers    prose -> 24AAACB8755A2Z0, 24AAACB8755A4ZY
                     refs  -> 24AAACB8755A2Z0, 24AAACB8755A4ZY   (agree: the prose was grounded)
    RKeCom Services  prose -> [] on one run, a FABRICATED GSTIN on another
                     refs  -> [] on both runs                    (the guard holds either way)

This is VT-777's rule in a second place: accept only what a real source said, never what the model
synthesised. It costs nothing when the model is right (the citations carry the same numbers) and it
is the entire defence when the model is wrong.

STILL HINTS-ONLY, and a cited GSTIN must ALSO sit in text echoing a distinctive token of the queried
name — AI Mode cites competitors, suppliers and generic "GST registration in Mumbai" pages. The
Sandbox GST verify remains the sole authoritative gate; nothing here can weaken or bypass it.

FAIL-SOFT EVERYWHERE (best-effort discovery — must NEVER block onboarding): missing credential,
network error, non-200, parse error, zero results → ``[]``. ``search_gstins`` never raises out.

Credential: ``DATAFORSEO_API_BASE64`` (pre-encoded HTTP Basic). Consumed OS-env → process, never
logged; error paths carry a status code only, never a response body that could echo it back.

Endpoint note: the published docs show these paths WITH a trailing slash. The API returns 404 with
it. The path below is deliberately slash-free.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_KEY_ENV = "DATAFORSEO_API_BASE64"
_AI_MODE_URL = "https://api.dataforseo.com/v3/serp/google/ai_mode/live/advanced"
_LOCATION_CODE = 2356  # India — the fallback when a tenant city does not resolve
_TASK_OK = 20000       # DataForSEO task-level success
_LOCATIONS_URL = "https://api.dataforseo.com/v3/serp/google/locations/in"
_locations: dict[str, int] | None = None  # lazy, per-process: city -> location_code
_LANGUAGE_CODE = "en"
_TIMEOUT_S = 60.0
_MIN_QUERY_LEN = 5

_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")

# Mirrors entity_match._GENERIC_NAME_TOKENS — tokens that carry no distinctive identity, so
# "RKeCom Services" is not treated as matching every "...Services" in the answer text.
_GENERIC_TOKENS = frozenset({
    "services", "service", "pvt", "ltd", "private", "limited", "the", "and", "co", "company",
    "llp", "opc", "inc", "enterprises", "enterprise", "solutions", "solution", "india", "indian",
    "store", "stores", "shop", "trading", "traders", "industries", "corporation", "group",
    "biz", "ventures", "venture", "holdings", "global", "online", "mart", "hub", "world",
    "international",
})

_CACHE_TTL_S = 6 * 3600
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def configured() -> bool:
    """True when the credential is present. Names→booleans only; never reads the value out."""
    return bool(os.environ.get(_KEY_ENV, "").strip())


def _significant_tokens(name: str) -> set[str]:
    """The distinctive tokens a candidate's surrounding text must echo. Empty when a name is ALL
    generic — then we do NOT over-filter (same posture as entity_match._significant_tokens)."""
    return {
        t for t in re.findall(r"[a-z0-9]+", (name or "").lower())
        if len(t) >= 3 and t not in _GENERIC_TOKENS
    }


def _iter_strings(node: Any) -> list[str]:
    """Every string in the response tree — AI Mode puts its answer across nested item blocks."""
    out: list[str] = []
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _references(payload: Any) -> list[Any]:
    """The cited-source blocks of every AI Mode item. ``[]`` on any unexpected shape (fail-soft).

    Everything outside this array is the model's own synthesised prose and is deliberately
    discarded — see the module docstring for the GSTIN it invented."""
    out: list[Any] = []
    try:
        for task in (payload or {}).get("tasks") or []:
            for result in task.get("result") or []:
                for item in result.get("items") or []:
                    out.extend(item.get("references") or [])
    except (AttributeError, TypeError):
        return []
    return out


def _extract_from_references(refs: list[Any], sig: set[str]) -> list[dict[str, str]]:
    """GSTINs carried by a CITED SOURCE whose text also echoes a distinctive token of the name.

    Both conditions are load-bearing. Citation-only would accept a GSTIN off a cited competitor's
    page; name-pairing-only would accept the model's invented prose. Together they mean: a real
    source said this number, about this business."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for ref in refs:
        blob = " ".join(v for v in _iter_strings(ref) if isinstance(v, str))
        hits = _GSTIN_RE.findall(blob.upper())
        if not hits:
            continue
        # No distinctive tokens (an all-generic business name) → do not over-filter.
        if sig and not any(t in blob.lower() for t in sig):
            continue
        title = ""
        if isinstance(ref, dict):
            title = str(ref.get("title") or "")
        for gstin in hits:
            if gstin in seen:
                continue
            seen.add(gstin)
            out.append({"company_name": title[:200], "state": "",
                        "gst_number": gstin, "context": blob[:300]})
    return out


def search_gstins(name: str, city: str = "", *, fetch_fn: Any = None) -> list[dict[str, str]]:
    """One AI Mode lookup → ``[{company_name, state, gst_number, context}]``. NEVER raises out.

    ``fetch_fn(query) -> dict`` is injectable for unit tests (no network / no credential)."""
    name = (name or "").strip()
    if len(name) < _MIN_QUERY_LEN:
        return []
    cache_key = f"{name.lower()}|{(city or '').lower().strip()}"
    hit = _cache.get(cache_key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL_S:
        return hit[1]

    if fetch_fn is None and not configured():
        return []  # fail-open: no credential → leg skipped, cascade falls through

    # KEYWORD form, not a natural-language question, and the CITY DOES NOT GO IN THE KEYWORD.
    # This is a SERP engine: phrasing decides what Google retrieves, and a city term in the query
    # RESTRICTS the result set instead of merely preferring local ones. Measured on
    # "Sundaram Book Store" (truth: 27AADCS7829K1ZT, Sundaram Multi Pap Ltd — "Sundaram" is that
    # company's brand, so the shop name and the registered name differ):
    #
    #   keyword                                geo                       result
    #   "... GSTIN"                            Mumbai                    HIT, 7 refs
    #   "... GSTIN"                            India                     HIT, 6 refs
    #   "... GSTN"                             Mumbai                    miss
    #   "... GSTIN GST number"                 either                    miss
    #   "What is the GSTIN ... of ...?"        India                     miss, 2 refs
    #
    # So: business name first, ONE qualifier, nothing else. "GSTIN" beats "GSTN" (GSTN is the tax
    # network, GSTIN the number) and survives geo-targeting where GSTN did not. Extra qualifier
    # words starve retrieval — "GSTIN GST number" lost the answer entirely.
    query = f"{name} GSTIN"
    try:
        payload = fetch_fn(query) if fetch_fn is not None else _ai_mode_fetch(query, city)
    except Exception:  # noqa: BLE001 — discovery must never block onboarding
        logger.warning("dataforseo: AI Mode fetch failed (degrade to none)", exc_info=True)
        return []
    try:
        rows = _extract_from_references(_references(payload), _significant_tokens(name))
    except Exception:  # noqa: BLE001
        logger.warning("dataforseo: parse failed (degrade to none)", exc_info=True)
        return []
    _cache[cache_key] = (time.time(), rows)
    return rows


def _location_code_for(city: str) -> int | None:
    """Resolve a bare city name to DataForSEO's ``location_code``. ``None`` when unknown.

    Geo-targeting is NOT a nicety on this leg, it decides the answer. Measured on
    "Sundaram Book Store" (truth 27AADCS7829K1ZT), 4 runs each:

        location_code 2356 (India-wide)   ->  0/4 hits
        Mumbai                            ->  4/4 hits

    So the tenant's city must reach the request. It cannot go in the KEYWORD (that restricts the
    result set instead of preferring local ones — Fazal 2026-08-22), and ``location_name`` needs a
    full "City,Region,Country" string that an IP-derived city name does not give us: "Mumbai,India"
    is rejected outright with `Invalid Field: 'location_name'`. So we resolve the city against
    DataForSEO's own location list and pass the numeric code.

    The list is ~24k rows / 3.7MB but answers in ~1s and is cached for the process lifetime. Any
    failure returns None and the caller falls back to India-wide — a slow or broken lookup must
    never cost us the discovery."""
    global _locations
    city = (city or "").strip().lower()
    if not city:
        return None
    if _locations is None:
        try:
            import httpx

            resp = httpx.get(
                _LOCATIONS_URL,
                headers={"Authorization": f"Basic {os.environ.get(_KEY_ENV, '').strip()}"},
                timeout=_TIMEOUT_S,
            )
            if resp.is_error:
                raise RuntimeError(f"dataforseo: locations returned HTTP {resp.status_code}")
            rows = ((resp.json() or {}).get("tasks") or [{}])[0].get("result") or []
            table: dict[str, int] = {}
            for row in rows:
                name = str(row.get("location_name") or "")
                code = row.get("location_code")
                if not name or not isinstance(code, int):
                    continue
                key = name.split(",")[0].strip().lower()
                # Prefer a City row; never let a broader region overwrite one already claimed.
                if key and (key not in table or row.get("location_type") == "City"):
                    table.setdefault(key, code)
            _locations = table
        except Exception:  # noqa: BLE001 — geo resolution is best-effort
            logger.warning("dataforseo: location lookup failed (falling back to India-wide)", exc_info=True)
            _locations = {}
    return _locations.get(city)


def _ai_mode_fetch(query: str, city: str = "") -> dict[str, Any]:
    """POST one live AI Mode task. Raises on transport/HTTP error → ``search_gstins`` degrades.

    ``city`` is the tenant's locale (expected to come from their IP, not from anything they type).
    It rides DataForSEO's geo targeting so Google WEIGHTS local results — a preference, not a
    filter. It is deliberately kept OUT of the keyword, where it would restrict instead."""
    import httpx

    payload: dict[str, Any] = {
        "keyword": query,
        "language_code": _LANGUAGE_CODE,
        "location_code": _location_code_for(city) or _LOCATION_CODE,
    }
    resp = httpx.post(
        _AI_MODE_URL,
        headers={
            "Authorization": f"Basic {os.environ.get(_KEY_ENV, '').strip()}",
            "Content-Type": "application/json",
        },
        json=[payload],
        timeout=_TIMEOUT_S,
    )
    if resp.is_error:
        # Status only. Never raise_for_status(), never interpolate the body: the credential rides
        # in the Authorization header and `search_gstins` logs this with exc_info=True.
        raise RuntimeError(
            f"dataforseo: AI Mode returned HTTP {resp.status_code} "
            f"(credential {'present' if configured() else 'absent'}; body withheld)"
        )
    return resp.json() or {}
