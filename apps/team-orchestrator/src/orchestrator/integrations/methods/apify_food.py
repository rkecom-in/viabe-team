"""VT-61 / VT-6 Method 6+7 — Swiggy + Zomato food-platform context (Apify).

Two tenant-OWN-business context fetches → L1 business_profile. Context-only (no
dedup, no ledger). Cowork plan APPROVED 2026-06-01 (strip boundary + D1/D2 rulings).

SWIGGY (``infoweaver/my-actor``) — listing data only: rating, cuisines,
cost-for-two, delivery time, offer, isAdvertisement. No PII, no LLM. Allowlist →
business_profile.swiggy_context.

ZOMATO (``easyapi/zomato-restaurant-reviews-scraper``) — the rented actor's RAW
output is verbatim reviewText + reviewer identity (userName/profileUrl/userId/pic).
HARD RULE (CL-390): STRIP at ingest, persist AGGREGATE only. Strip boundary:
  1. actor → raw review items.
  2. reviewer identity DROPPED immediately — only (star_rating, reviewText) kept
     transiently (never logged, never persisted).
  3. sentiment_distribution = DETERMINISTIC from the star distribution (D2 — NO LLM,
     no transmission, no variance).
  4. themes = LLM over reviewText ONLY (identity already stripped) → ABSTRACT labels
     ("slow delivery", "great biryani"), NEVER verbatim quotes (those can carry
     self-disclosed PII). The LLM transmission is GATED on owner_inputs (D1/CL-390);
     no consent → themes skipped (deterministic aggregate still persisted).
  5. reviewText DISCARDED after derivation (transient, like vision CL-330).
  6. persist ONLY {overall_rating, review_count, sentiment_distribution, themes[]}
     → business_profile.zomato_context.

GRACEFUL-DEGRADE (scrape sources fragile, esp. Zomato): missing query / absent
token / actor failure / empty → log + dropped=1, never raised. Apify token =
APIFY_API_TOKEN (.viabe/secrets/apify.env); actor call + theme LLM both injectable
for tests. CL-422 dev = synthetic only.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml
from anthropic import Anthropic

from orchestrator.integrations.methods._image_adapter import IngestionSummary
from orchestrator.knowledge.l1 import upsert_business_profile

logger = logging.getLogger(__name__)

_MODELS_YAML = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
_THEME_PROMPT = (
    Path(__file__).resolve().parents[2] / "agent" / "prompts" / "zomato_themes_v1.md"
)
_SWIGGY_ACTOR = "thirdwatch~swiggy-scraper"  # VT-110: the account's real Swiggy actor (infoweaver~my-actor was a wrong/generic id → fail-soft EMPTY context)
_ZOMATO_ACTOR = "easyapi~zomato-restaurant-reviews-scraper"
_TOKEN_ENV = "APIFY_API_TOKEN"
_MAX_OUTPUT_TOKENS = 1024

# (actor_id, run_input, token) -> dataset items.
FetchFn = Callable[[str, dict[str, Any], str], list[dict[str, Any]]]
# (review_texts) -> theme dicts ({label, sentiment, mentions}).
ThemeFn = Callable[[list[str]], list[dict[str, Any]]]


def _default_fetch(actor: str, run_input: dict[str, Any], token: str) -> list[dict[str, Any]]:
    """Real Apify call (run-sync-get-dataset-items REST endpoint, httpx)."""
    import httpx

    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    resp = httpx.post(url, params={"token": token}, json=run_input, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _read_profile(tenant_id: UUID | str) -> dict[str, Any]:
    """Current business_profile attributes (RLS-scoped); {} if none yet."""
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    if row is None:
        return {}
    attrs = row["attributes"] if isinstance(row, dict) else row[0]
    return dict(attrs) if attrs else {}


def _merge_context(tenant_id: UUID | str, key: str, ctx: dict[str, Any]) -> None:
    """Merge ``ctx`` under ``key`` into business_profile (preserves sibling keys)."""
    upsert_business_profile(tenant_id, {**_read_profile(tenant_id), key: ctx})


def _query_input(business_name: str | None, locality: str | None, place_url: str | None,
                 base: dict[str, Any]) -> dict[str, Any] | None:
    if place_url:
        return {**base, "startUrls": [{"url": place_url}]}
    if business_name:
        q = f"{business_name} {locality}".strip() if locality else business_name
        return {**base, "search": q, "searchStringsArray": [q]}
    return None


# --- Swiggy (listing, no PII) -------------------------------------------------

def _swiggy_aggregate(item: dict[str, Any]) -> dict[str, Any]:
    """Allowlist — listing fields only (no PII present in Swiggy listings). Reads BOTH the
    thirdwatch~swiggy-scraper snake_case keys (VT-110, the real account actor) and the legacy
    camelCase keys, so a parser shape change can't silently drop a field (the fail-soft trap)."""
    return {
        "rating": item.get("rating") or item.get("avgRating") or item.get("google_rating"),
        "cuisines": item.get("cuisines") or item.get("cuisine"),
        "cost_for_two": item.get("costForTwo") or item.get("price") or item.get("cost_for_two") or item.get("cost_for_two_rupees"),
        "delivery_time": item.get("deliveryTime") or item.get("sla") or item.get("delivery_time") or item.get("delivery_time_minutes"),
        "offer": item.get("offer") or item.get("aggregatedDiscountInfo") or item.get("offers"),
        "is_advertisement": bool(item.get("isAdvertisement", item.get("is_promoted", False))),
    }


def ingest_swiggy(
    tenant_id: UUID | str,
    *,
    business_name: str | None = None,
    locality: str | None = None,
    place_url: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    fetch_fn: FetchFn | None = None,
    token: str | None = None,
) -> IngestionSummary:
    """Fetch Swiggy listing context → L1 business_profile.swiggy_context. Counts only."""
    now = now or datetime.now(UTC)
    run_input = _query_input(business_name, locality, place_url, {"maxItems": 1})
    if run_input is None:
        logger.warning("ingest_swiggy: tenant=%s no query — skip", tenant_id)
        return IngestionSummary(0, 0, 0, 1)
    token = token or os.environ.get(_TOKEN_ENV)
    if not token:
        logger.warning("ingest_swiggy: tenant=%s %s absent — skip", tenant_id, _TOKEN_ENV)
        return IngestionSummary(0, 0, 0, 1)
    fetch = fetch_fn or _default_fetch
    try:
        items = fetch(_SWIGGY_ACTOR, run_input, token)
    except Exception as exc:  # noqa: BLE001 — fragile scrape; degrade
        logger.warning("ingest_swiggy: tenant=%s actor failure (%s) — skip", tenant_id, type(exc).__name__)
        return IngestionSummary(0, 0, 0, 1)
    if not items:
        logger.info("ingest_swiggy: tenant=%s no listing — skip", tenant_id)
        return IngestionSummary(0, 0, 0, 1)
    ctx = _swiggy_aggregate(items[0])
    ctx["acquired_via"] = "apify_swiggy"
    ctx["last_updated"] = now.isoformat()
    _merge_context(tenant_id, "swiggy_context", ctx)
    logger.info("ingest_swiggy: tenant=%s context written (rating=%s)", tenant_id, ctx.get("rating"))

    # VT-325: ALSO persist the per-listing row + emit (distinct from the aggregate
    # swiggy_context). external_listing_id = the Swiggy restaurant id (fallback:
    # place_url). Best-effort — never break the aggregate ingest.
    item = items[0]
    ext_id = item.get("id") or item.get("restaurantId") or place_url
    if ext_id:
        try:
            from orchestrator.integrations.platform_listings import (
                write_platform_listing,
            )

            write_platform_listing(
                tenant_id, "swiggy", str(ext_id),
                rating=ctx.get("rating"),
                attributes={  # CL-390: structured non-PII facts only
                    "cuisines": ctx.get("cuisines"),
                    "cost_for_two": ctx.get("cost_for_two"),
                    "delivery_time": ctx.get("delivery_time"),
                },
            )
        except Exception as exc:  # noqa: BLE001 — per-listing best-effort
            logger.warning(
                "ingest_swiggy: per-listing write failed (%s) — aggregate unaffected",
                type(exc).__name__,
            )
    return IngestionSummary(entries_extracted=1, committed=1, pending_clarification=0, dropped=0)


# --- Zomato (strip → aggregate) -----------------------------------------------

def _sentiment_distribution(ratings: list[float]) -> dict[str, int]:
    """Deterministic sentiment from star ratings (D2 — no LLM)."""
    pos = sum(1 for r in ratings if r >= 4)
    neu = sum(1 for r in ratings if r == 3)
    neg = sum(1 for r in ratings if r <= 2)
    return {"positive": pos, "neutral": neu, "negative": neg}


def _zomato_review_rating(item: dict[str, Any]) -> float:
    """Extract a review's numeric star rating. VT-110 live canary: easyapi~zomato puts the number
    in ``ratingV2`` (a STRING like '5'); ``rating`` is a dict (``{'entities': [...]}``) — NOT a
    number, so the old ``float(item['rating'])`` read silently yielded 0 → overall_rating None +
    all-zero sentiment. Try ratingV2 first, then the legacy numeric ``rating``/``reviewRating``.
    Returns 0.0 if unparseable (drops to no-rating, never crashes)."""
    for v in (item.get("ratingV2"), item.get("rating"), item.get("reviewRating")):
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return 0.0


def _resolve_theme_model() -> str:
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["zomato_theme_extraction"][slot])


def _default_themes(review_texts: list[str]) -> list[dict[str, Any]]:
    """LLM theme clustering over IDENTITY-STRIPPED review text → abstract labels."""
    if not review_texts:
        return []
    client = Anthropic()
    model = _resolve_theme_model()
    base = _THEME_PROMPT.read_text(encoding="utf-8")
    joined = "\n".join(f"- {t}" for t in review_texts if t)
    resp = client.messages.create(
        model=model, max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"{base}\n\nREVIEW TEXTS:\n{joined}\n"}]}],
    )
    raw = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    themes = parsed.get("themes") if isinstance(parsed, dict) else None
    return themes if isinstance(themes, list) else []


def ingest_zomato(
    tenant_id: UUID | str,
    *,
    business_name: str | None = None,
    locality: str | None = None,
    place_url: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    fetch_fn: FetchFn | None = None,
    token: str | None = None,
    consent_check: Callable[[UUID], bool] | None = None,
    theme_fn: ThemeFn | None = None,
) -> IngestionSummary:
    """Fetch Zomato reviews → STRIP identity → aggregate (sentiment + themes) → L1.

    Reviewer identity + verbatim text NEVER persisted. Theme-LLM transmission gated
    on owner_inputs (D1); no consent → deterministic aggregate only (themes=[]).
    """
    now = now or datetime.now(UTC)
    run_input = _query_input(business_name, locality, place_url, {"maxReviews": 100})
    if run_input is None:
        logger.warning("ingest_zomato: tenant=%s no query — skip", tenant_id)
        return IngestionSummary(0, 0, 0, 1)
    token = token or os.environ.get(_TOKEN_ENV)
    if not token:
        logger.warning("ingest_zomato: tenant=%s %s absent — skip", tenant_id, _TOKEN_ENV)
        return IngestionSummary(0, 0, 0, 1)
    fetch = fetch_fn or _default_fetch
    try:
        items = fetch(_ZOMATO_ACTOR, run_input, token)
    except Exception as exc:  # noqa: BLE001 — fragile scrape; degrade
        logger.warning("ingest_zomato: tenant=%s actor failure (%s) — skip", tenant_id, type(exc).__name__)
        return IngestionSummary(0, 0, 0, 1)
    if not items:
        logger.info("ingest_zomato: tenant=%s no reviews — skip", tenant_id)
        return IngestionSummary(0, 0, 0, 1)

    # STRIP boundary: keep ONLY (rating, text) transiently; drop ALL reviewer identity.
    ratings: list[float] = []
    texts: list[str] = []
    for it in items:
        ratings.append(_zomato_review_rating(it))
        txt = it.get("reviewText") or it.get("text") or it.get("review")
        if txt:
            texts.append(str(txt))

    rated = [r for r in ratings if r > 0]
    overall_rating = round(sum(rated) / len(rated), 2) if rated else None
    sentiment = _sentiment_distribution(rated)

    # Themes: LLM over identity-stripped text, gated on owner_inputs (D1/CL-390).
    if consent_check is None:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        consent_check = _owner_inputs_enabled
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    themes: list[dict[str, Any]] = []
    if consent_check(tid):
        themer = theme_fn or _default_themes
        try:
            themes = themer(texts)
        except Exception as exc:  # noqa: BLE001 — theme LLM optional; degrade to aggregate-only
            logger.warning("ingest_zomato: tenant=%s theme extraction failed (%s)", tenant_id, type(exc).__name__)
            themes = []
    else:
        logger.info("ingest_zomato: tenant=%s owner_inputs off — themes skipped (aggregate only)", tenant_id)

    ctx = {
        "overall_rating": overall_rating,
        "review_count": len(items),
        "sentiment_distribution": sentiment,
        "themes": themes,
        "acquired_via": "apify_zomato",
        "last_updated": now.isoformat(),
    }
    _merge_context(tenant_id, "zomato_context", ctx)
    # reviewText + identity discarded here (only `ctx` persisted). Log counts only.
    logger.info(
        "ingest_zomato: tenant=%s aggregate written (reviews=%d themes=%d)",
        tenant_id, len(items), len(themes),
    )

    # VT-325: ALSO persist the per-listing row + emit. Zomato is reviews-based (no
    # listing object), so external_listing_id = the restaurant URL (stable, non-PII).
    # CL-390 / Cowork Q2: store ONLY the deterministic non-PII aggregate (rating +
    # review_count + sentiment counts) — NOT the review-derived `themes` (those are a
    # deferred separate row with scrub-at-ingest). Best-effort. Requires place_url.
    if place_url:
        try:
            from orchestrator.integrations.platform_listings import (
                write_platform_listing,
            )

            write_platform_listing(
                tenant_id, "zomato", str(place_url),
                rating=overall_rating,
                attributes={
                    "review_count": len(items),
                    "sentiment_distribution": sentiment,
                },
            )
        except Exception as exc:  # noqa: BLE001 — per-listing best-effort
            logger.warning(
                "ingest_zomato: per-listing write failed (%s) — aggregate unaffected",
                type(exc).__name__,
            )
    return IngestionSummary(entries_extracted=len(items), committed=1, pending_clarification=0, dropped=0)


__all__ = ["ingest_swiggy", "ingest_zomato"]
