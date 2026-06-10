"""VT-366 Gap-2a — Auto-Discovery source adapters (GBP + website + Serper-stub).

Each ``discover_*`` fetches one public source, writes the fields it found to the tenant's DRAFT
(``draft_profile.write_draft`` — owner-confirmed later, NEVER asserted as fact here), and returns a
``SourceResult`` carrying its cost. Fail-soft is the ENGINE's job (it catches); a source raising is
fine. CL-390: only the business's OWN public listing/site — no third-party PII. Cost is per-source +
bounded; the engine enforces the run ceiling.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from orchestrator.onboarding.draft_profile import write_draft

logger = logging.getLogger(__name__)

# Per-source cost (USD). GBP = compass/crawler-google-places (~$4/1k places → 1 place ≈ $0.004);
# website = one Haiku extract (~$0.001). Serper deferred (key-gated). Used for the engine's ceiling.
_GBP_COST_USD = 0.004
_WEBSITE_COST_USD = 0.001
_HTTP_TIMEOUT = 20.0
_WEBSITE_MAX_CHARS = 12000  # cap the page text fed to the LLM (cost + prompt-injection surface)
_EXTRACT_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str  # "ok" | "empty" | "skipped" | "error"
    cost_usd: float = 0.0
    fields: dict[str, Any] = field(default_factory=dict)
    website: str | None = None  # GBP exposes the business's website for the website source


def _gbp_query(seed: dict[str, Any]) -> str | None:
    name = (seed.get("business_name") or "").strip()
    city = (seed.get("city") or "").strip()
    if not name:
        return None
    return f"{name} {city}".strip()


def discover_gbp(
    tenant_id: UUID | str,
    seed: dict[str, Any],
    *,
    token: str | None = None,
    fetch_fn: Callable[[dict[str, Any], str], list[dict[str, Any]]] | None = None,
) -> SourceResult:
    """GBP via compass/crawler-google-places (maxReviews:0 — profile only, no reviews/PII). Extracts
    the business's own listing fields INCLUDING its website (which the website source then fetches)."""
    token = token or os.environ.get("APIFY_API_TOKEN")
    if not token:
        return SourceResult("gbp", "skipped")
    query = _gbp_query(seed)
    if not query:
        return SourceResult("gbp", "skipped")
    from orchestrator.integrations.methods.apify_gbp import _default_fetch

    fetch = fetch_fn or _default_fetch
    run_input = {"maxReviews": 0, "maxImages": 0, "language": "en", "searchStringsArray": [query]}
    items = fetch(run_input, token)
    if not items:
        return SourceResult("gbp", "empty", cost_usd=_GBP_COST_USD)
    place = items[0]
    website = place.get("website") or place.get("url")
    fields = {
        k: v
        for k, v in {
            "business_name": place.get("title"),
            "category": place.get("categoryName"),
            "city": place.get("city"),
            "rating": place.get("totalScore"),
            "website": website,
        }.items()
        if v is not None
    }
    if fields:
        write_draft(tenant_id, fields, source="gbp")
    return SourceResult("gbp", "ok" if fields else "empty", cost_usd=_GBP_COST_USD, fields=fields, website=website)


def discover_website(
    tenant_id: UUID | str,
    seed: dict[str, Any],
    *,
    url: str | None = None,
    fetch_fn: Callable[[str], str] | None = None,
    extract_fn: Callable[[str], dict[str, Any]] | None = None,
) -> SourceResult:
    """Fetch the business's OWN website (URL from GBP, or owner-provided) → Haiku-extract a small
    set of context fields (about / services). Skipped (not an error) when there's no URL."""
    url = url or seed.get("website")
    if not url:
        return SourceResult("website", "skipped")
    try:
        text = (fetch_fn or _fetch_website)(url)
    except Exception as exc:  # noqa: BLE001 — fragile network; degrade
        logger.warning("discover_website: fetch failed url=%s (%s)", url, type(exc).__name__)
        return SourceResult("website", "error")
    if not text.strip():
        return SourceResult("website", "empty", cost_usd=0.0)
    fields = (extract_fn or _extract_website)(text[:_WEBSITE_MAX_CHARS])
    fields = {k: v for k, v in fields.items() if v}
    if fields:
        write_draft(tenant_id, fields, source="website")
    return SourceResult("website", "ok" if fields else "empty", cost_usd=_WEBSITE_COST_USD, fields=fields)


def discover_serper(tenant_id: UUID | str, seed: dict[str, Any]) -> SourceResult:
    """Serper.dev web search — DEFERRED fast-follow. Key-gated graceful-degrade: SERPER_API_KEY
    absent → cleanly skipped + logged (the DBOS-conductor opt-in pattern), NEVER an error."""
    if not os.environ.get("SERPER_API_KEY"):
        logger.info("discover_serper: SERPER_API_KEY absent — source skipped (fast-follow)")
        return SourceResult("serper", "skipped")
    # Wiring lands with the key (a separate fast-follow PR + its own live canary).
    return SourceResult("serper", "skipped")


def _fetch_website(url: str) -> str:
    """GET the page; return a crude text strip (tags removed). Best-effort, short timeout."""
    import re

    import httpx

    resp = httpx.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": "ViabeBot/1.0"})
    resp.raise_for_status()
    html = resp.text
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _extract_website(text: str) -> dict[str, Any]:
    """Haiku-extract a small context set from the page text. Returns {} on any failure (fail-soft)."""
    from anthropic import Anthropic

    prompt = (
        "From this business website text, extract a short factual summary as JSON with keys "
        '"about" (1-2 sentences on what the business does) and "services" (a short list of '
        "offerings, max 6). Use ONLY what the text states; if unknown, use null/[]. No prose, JSON only.\n\n"
        f"TEXT:\n{text}"
    )
    try:
        resp = Anthropic().messages.create(
            model=_EXTRACT_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        import json

        raw = resp.content[0].text if resp.content else "{}"
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1]) if start != -1 and end != -1 else {}
        return {"about": data.get("about"), "services": data.get("services")}
    except Exception as exc:  # noqa: BLE001 — LLM/parse fragile; degrade to no website fields
        logger.warning("discover_website: extract failed (%s)", type(exc).__name__)
        return {}
