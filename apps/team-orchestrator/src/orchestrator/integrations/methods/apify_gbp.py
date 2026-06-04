"""VT-62 / VT-6 Method 8 — Google Business Profile aggregate context (Apify).

Pulls the tenant's OWN GBP AGGREGATE context (rating, review count, category,
price level) via the Apify actor ``compass/crawler-google-places`` with
``maxReviews=0`` — for L1 business-profile context, NOT customer data.

PRIVACY (CL-390, hard rule): NO verbatim reviews and NO reviewer identity are
EVER read or stored. Double-guarded: (1) ``maxReviews=0`` so the actor returns
none, and (2) ``_aggregate`` reads ONLY an explicit allowlist of aggregate fields
— review/reviewer keys are never copied even if present. The no-PII negative test
asserts this against a synthetic response that DOES contain reviews + reviewer PII.

Tool-registry intent (Cowork VT-62, the pluggable-tool model): {capability =
"fetch a tenant's Google Business Profile AGGREGATE context", when-to-use =
"onboarding / periodic business-context refresh", inputs = business name+locality
OR a place URL}. Context-only → L1 (``upsert_business_profile``); no dedup, no
ledger, no customers row.

GRACEFUL-DEGRADE (Cowork): scrape sources are fragile — a missing query, absent
token, actor failure, or empty result is LOGGED + SKIPPED (dropped=1), never raised
(the agent keeps operating without GBP context). Apify token = APIFY_API_TOKEN
(.viabe/secrets/apify.env). The actor call is injectable (``fetch_fn``) so tests
run without network/token. CL-422: dev = synthetic only.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from orchestrator.integrations.methods._image_adapter import IngestionSummary
from orchestrator.knowledge.l1 import upsert_business_profile

logger = logging.getLogger(__name__)

_ACTOR = "compass~crawler-google-places"  # '~' is the Apify REST path separator
_APIFY_URL = f"https://api.apify.com/v2/acts/{_ACTOR}/run-sync-get-dataset-items"
_TOKEN_ENV = "APIFY_API_TOKEN"
ACQUIRED_VIA = "apify_gbp"

# (run_input, token) -> the actor's dataset items (list of place dicts).
FetchFn = Callable[[dict[str, Any], str], list[dict[str, Any]]]


def _aggregate(place: dict[str, Any]) -> dict[str, Any]:
    """Read ONLY allowlisted aggregate fields — never reviews/reviewer identity.

    This allowlist IS the privacy boundary (CL-390): any field not named here —
    including ``reviews``, ``reviewsTags`` carrying text, reviewer ``name`` /
    ``reviewerUrl`` / ``reviewerId`` — is never copied into what gets persisted.
    """
    return {
        "gbp_title": place.get("title"),
        "rating": place.get("totalScore"),
        "reviews_count": place.get("reviewsCount"),
        "category": place.get("categoryName"),
        "price_level": place.get("price"),
        "permanently_closed": bool(place.get("permanentlyClosed", False)),
        "neighborhood": place.get("neighborhood"),
        "city": place.get("city"),
    }


def _default_fetch(run_input: dict[str, Any], token: str) -> list[dict[str, Any]]:
    """Real Apify call (run-sync-get-dataset-items REST endpoint, httpx)."""
    import httpx

    resp = httpx.post(_APIFY_URL, params={"token": token}, json=run_input, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _existing_business_profile(tenant_id: UUID | str) -> dict[str, Any]:
    """Current business_profile attributes (RLS-scoped); {} if none yet.

    Read-merge-write so the GBP refresh nests under a 'gbp_context' key WITHOUT
    clobbering sibling keys (archetype / owner persona / operating notes), since
    upsert_business_profile full-replaces attributes. The read→upsert window is
    small and GBP refresh is infrequent — acceptable Phase-1 (no concurrent
    onboarding write expected).
    """
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    if row is None:
        return {}
    attrs = row["attributes"] if isinstance(row, dict) else row[0]
    return dict(attrs) if attrs else {}


def ingest_gbp(
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
    """Fetch GBP aggregate context → merge into L1 business_profile. Counts only.

    Provide either ``place_url`` (preferred, exact) or ``business_name`` (+optional
    ``locality``). tenant_id from invocation context (P3). Graceful-degrade: any
    missing input / token / actor failure / empty result → dropped=1, no raise.
    """
    now = now or datetime.now(UTC)
    run_input: dict[str, Any] = {
        "maxReviews": 0, "maxImages": 0, "language": "en",
        "maxCrawledPlacesPerSearch": 1,
    }
    if place_url:
        run_input["startUrls"] = [{"url": place_url}]
    elif business_name:
        query = f"{business_name} {locality}".strip() if locality else business_name
        run_input["searchStringsArray"] = [query]
    else:
        logger.warning(
            "ingest_gbp: tenant=%s no GBP query (need place_url or business_name) — skip",
            tenant_id,
        )
        return IngestionSummary(0, 0, 0, 1)

    token = token or os.environ.get(_TOKEN_ENV)
    if not token:
        logger.warning("ingest_gbp: tenant=%s %s absent — skip", tenant_id, _TOKEN_ENV)
        return IngestionSummary(0, 0, 0, 1)

    fetch = fetch_fn or _default_fetch
    try:
        items = fetch(run_input, token)
    except Exception as exc:  # noqa: BLE001 — scrape sources fragile; degrade, don't crash
        logger.warning(
            "ingest_gbp: tenant=%s actor failure (%s) — skip", tenant_id, type(exc).__name__
        )
        return IngestionSummary(0, 0, 0, 1)

    if not items:
        logger.info("ingest_gbp: tenant=%s no place found — skip", tenant_id)
        return IngestionSummary(0, 0, 0, 1)

    agg = _aggregate(items[0])
    agg["acquired_via"] = ACQUIRED_VIA
    agg["last_updated"] = now.isoformat()

    merged = {**_existing_business_profile(tenant_id), "gbp_context": agg}
    upsert_business_profile(tenant_id, merged)
    logger.info(
        "ingest_gbp: tenant=%s context written (rating=%s reviews_count=%s)",
        tenant_id, agg.get("rating"), agg.get("reviews_count"),  # aggregate numbers, not PII
    )

    # VT-325: ALSO persist the per-listing row + emit (distinct from the aggregate
    # business_profile above). GBP = one listing per tenant (the business's own
    # place); external_listing_id = Google placeId. Best-effort: a per-listing
    # failure must NOT break the aggregate ingest.
    place = items[0]
    ext_id = place.get("placeId") or place.get("fid") or place.get("cid")
    if ext_id:
        try:
            from orchestrator.integrations.platform_listings import (
                write_platform_listing,
            )

            write_platform_listing(
                tenant_id, "gbp", str(ext_id),
                rating=place.get("totalScore"),
                attributes={  # CL-390: structured non-PII facts only
                    "name": place.get("title"),
                    "category": place.get("categoryName"),
                    "city": place.get("city"),
                    "neighborhood": place.get("neighborhood"),
                    "permanently_closed": bool(place.get("permanentlyClosed", False)),
                },
            )
        except Exception as exc:  # noqa: BLE001 — per-listing best-effort
            logger.warning(
                "ingest_gbp: per-listing write failed (%s) — aggregate unaffected",
                type(exc).__name__,
            )
    return IngestionSummary(entries_extracted=1, committed=1, pending_clarification=0, dropped=0)


__all__ = ["ACQUIRED_VIA", "ingest_gbp"]
