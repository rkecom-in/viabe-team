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
_LOCATION_CODE = 2356  # India
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

    location = f" in {city}" if (city or "").strip() else ""
    query = f"What is the GSTIN (GST registration number) of {name}{location}?"
    try:
        payload = fetch_fn(query) if fetch_fn is not None else _ai_mode_fetch(query)
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


def _ai_mode_fetch(query: str) -> dict[str, Any]:
    """POST one live AI Mode task. Raises on transport/HTTP error → ``search_gstins`` degrades."""
    import httpx

    resp = httpx.post(
        _AI_MODE_URL,
        headers={
            "Authorization": f"Basic {os.environ.get(_KEY_ENV, '').strip()}",
            "Content-Type": "application/json",
        },
        json=[{
            "keyword": query,
            "language_code": _LANGUAGE_CODE,
            "location_code": _LOCATION_CODE,
        }],
        timeout=_TIMEOUT_S,
    )
    if resp.is_error:
        # Status only. Never raise_for_status() and never interpolate the body: the credential
        # rides in the Authorization header and `search_gstins` logs this with exc_info=True.
        raise RuntimeError(
            f"dataforseo: AI Mode returned HTTP {resp.status_code} "
            f"(credential {'present' if configured() else 'absent'}; body withheld)"
        )
    return resp.json() or {}
