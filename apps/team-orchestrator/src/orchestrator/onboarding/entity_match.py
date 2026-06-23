"""VT-406 — entity-match at signup (verify-gated spine + discovery anchor).

The Sundaram bug: "Sundaram Book Store" (owner-typed) silently anchored to "Sundaram Multi Pap
Limited" (found). A wrong anchor poisons every downstream source. Fix: confirm the entity — against
the AUTHORITATIVE Sandbox GSTIN verify — before discovery asserts anything.

Flow (the synchronous spine; the web wizard / WhatsApp confirm drives it):
  fetch_candidates(name, city)  -> 0..N UNVERIFIED candidates (web-search + GBP). CANDIDATE
                                   GENERATORS ONLY — an LLM/web-scraped GSTIN is never shown as fact.
  owner picks one (or "none")  -> confirm_and_verify(tenant_id, gstin): round-trips the chosen GSTIN
                                   through Sandbox (verification.run_lookup, VT-361). ACTIVE => the
                                   tenant is gstin_verified + the entity anchor is persisted; the
                                   verified entity then SEEDS auto-discovery (async, non-blocking) so
                                   discovery keys off the verified entity, not the typed name.

Provenance split (CL-441 spirit): a field is "verified" only when Sandbox confirmed it; web/GBP
candidates are "found" (unconfirmed). NEVER render a web/LLM field as verified.

The HARD reject (no gstin_verified => no account/trial) is VT-408 (design-first, gated separately).
This module sets gstin_verified + the anchor and returns a status; it does NOT block account creation.

PII boundary (CL-390/425): only business-level identity (trade/legal name for a company, GSTIN,
locality, category) enters the anchor — never proprietor/director personal PII.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

# GSTIN: 2 state digits + PAN(5 letters + 4 digits + 1 letter) + 1 entity char + 'Z' + 1 checksum.
_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")

# Apify google-search actor for the candidate-GSTIN leg (Fazal: a plain Google "<name> gst number"
# surfaces it). Configurable; graceful-degrade to no candidates when token/actor absent.
_SEARCH_ACTOR = os.environ.get("APIFY_SEARCH_ACTOR", "apify~google-search-scraper")
_SEARCH_URL = f"https://api.apify.com/v2/acts/{_SEARCH_ACTOR}/run-sync-get-dataset-items"
_TOKEN_ENV = "APIFY_API_TOKEN"

# (query) -> list of result dicts (each may carry a 'description'/'title'/'url' with a GSTIN in text).
SearchFn = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class EntityCandidate:
    """One UNVERIFIED entity candidate surfaced for the owner to pick. A `candidate_gstin` is a
    HINT to round-trip through Sandbox — never shown as verified until confirm_and_verify says so."""

    trade_name: str | None
    source: str  # 'web' | 'gbp'
    candidate_gstin: str | None = None
    legal_name: str | None = None
    detail: str | None = None  # address/category — disambiguates "Sundaram"-class collisions


def fetch_candidates(
    business_name: str,
    city: str,
    *,
    search_fn: SearchFn | None = None,
    gbp_fetch_fn: Callable[[dict[str, Any], str], list[dict[str, Any]]] | None = None,
) -> list[EntityCandidate]:
    """Surface 0..N candidates. Web-search leg extracts candidate GSTINs by regex (then Sandbox is the
    authority); GBP leg adds a trade-name + locality candidate (no GSTIN). Graceful-degrade to [] when
    creds/actor are absent or the calls fail — entity-match must NEVER stall signup (VT-406 latency
    flag). Both legs are injectable for tests (no network/creds)."""
    name = (business_name or "").strip()
    if not name:
        return []
    candidates: list[EntityCandidate] = []
    candidates.extend(_web_candidates(name, city, search_fn))
    candidates.extend(_gbp_candidates(name, city, gbp_fetch_fn))
    # De-dup by (gstin or trade_name); keep the first (web GSTIN-bearing) seen.
    seen: set[str] = set()
    out: list[EntityCandidate] = []
    for c in candidates:
        key = (c.candidate_gstin or c.trade_name or "").upper()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _web_candidates(name: str, city: str, search_fn: SearchFn | None) -> list[EntityCandidate]:
    token = os.environ.get(_TOKEN_ENV)
    fn = search_fn
    if fn is None:
        if not token:
            return []
        fn = _default_search
    try:
        results = fn(f"{name} {city} GST number".strip())
    except Exception:  # noqa: BLE001 — fragile web search; degrade, never raise into signup
        logger.warning("entity_match: web candidate search failed (degrade to none)", exc_info=True)
        return []
    out: list[EntityCandidate] = []
    for r in results or []:
        blob = " ".join(str(r.get(k, "")) for k in ("title", "description", "url", "text"))
        for gstin in dict.fromkeys(_GSTIN_RE.findall(blob.upper())):  # ordered-unique
            out.append(
                EntityCandidate(
                    trade_name=_clean(r.get("title")),
                    source="web",
                    candidate_gstin=gstin,
                    detail=_clean(r.get("description")),
                )
            )
    return out


def _gbp_candidates(
    name: str, city: str, fetch_fn: Callable[[dict[str, Any], str], list[dict[str, Any]]] | None
) -> list[EntityCandidate]:
    token = os.environ.get(_TOKEN_ENV)
    if fetch_fn is None and not token:
        return []
    try:
        fetch = fetch_fn
        if fetch is None:
            # Import the default fetcher ONLY when actually needed — apify_gbp pulls the heavy
            # l1/ingestion import chain, which is absent in the dep-less smoke env; importing it
            # unconditionally (even with an injected fetch_fn) made the unit tests fail in CI.
            from orchestrator.integrations.methods.apify_gbp import _default_fetch

            fetch = _default_fetch
        items = fetch(
            {"maxReviews": 0, "maxImages": 0, "language": "en", "searchStringsArray": [f"{name} {city}".strip()]},
            token or "",
        )
    except Exception:  # noqa: BLE001
        logger.warning("entity_match: GBP candidate fetch failed (degrade)", exc_info=True)
        return []
    out: list[EntityCandidate] = []
    for place in (items or [])[:3]:
        title = _clean(place.get("title"))
        if not title:
            continue
        loc = _clean(place.get("city")) or _clean(place.get("address"))
        cat = _clean(place.get("categoryName"))
        out.append(
            EntityCandidate(
                trade_name=title,
                source="gbp",
                detail=" · ".join(x for x in (cat, loc) if x) or None,
            )
        )
    return out


def confirm_and_verify(
    tenant_id: UUID | str,
    gstin: str,
    *,
    lookup_fn: Callable[..., dict[str, Any]] | None = None,
    seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Round-trip the OWNER-CONFIRMED candidate GSTIN through Sandbox (verification.run_lookup, VT-361
    — attempt-capped, fail-closed, sets tenants.verification_status='gstin_verified' on ACTIVE). On
    verify: persist the entity anchor + seed async discovery with the VERIFIED entity. Returns the
    run_lookup result verbatim (ok / reason / status / name); a non-verified result does NOT raise and
    does NOT block — the hard reject is VT-408."""
    gstin = (gstin or "").strip().upper()
    if not _GSTIN_RE.fullmatch(gstin):
        return {"ok": False, "reason": "invalid_gstin_format", "status": "unverified"}

    run = lookup_fn
    if run is None:
        # Import the real verifier ONLY when not injected — verification pulls the psycopg/db chain,
        # absent in the dep-less smoke env (an unconditional import broke the injected-fn unit tests).
        from orchestrator.onboarding import verification

        run = verification.run_lookup
    result = run(tenant_id, gstin)
    if not result.get("ok"):
        return result  # vendor_down (retryable) or invalid_gstin (bad input) — caller/VT-408 decides

    name = result.get("name")
    _persist_anchor(tenant_id, gstin=gstin, verified_name=name)
    _seed_discovery(tenant_id, verified_name=name, gstin=gstin, seed=seed or {})
    return result


def _persist_anchor(
    tenant_id: UUID | str,
    *,
    gstin: str,
    verified_name: str | None,
    upsert_fn: Callable[[UUID | str, dict[str, Any]], Any] | None = None,
) -> None:
    """Persist the confirmed entity as the discovery anchor on the business_profile L1 entity. Provenance
    is inline (source='sandbox', verified=True) — VT-407 enriches; the VTR panel badges it 'verified'.
    Best-effort: a persist failure must not undo the verification (already committed by run_lookup).
    ``upsert_fn`` is injectable for tests (the default lazily imports l1 — psycopg-bound, absent in the
    dep-less smoke env, so it must not import at module load)."""
    try:
        upsert = upsert_fn
        if upsert is None:
            from orchestrator.knowledge.l1 import upsert_business_profile

            upsert = upsert_business_profile
        upsert(
            tenant_id,
            {
                "business_entity_anchor": {
                    "trade_name": verified_name,
                    "gstin": gstin,
                    "registry_kind": "gst",
                    "source": "sandbox",
                    "verified": True,
                    "confirmed_at": datetime.now(UTC).isoformat(),
                }
            },
        )
    except Exception:  # noqa: BLE001 — anchor is best-effort; verification already stands
        logger.exception("entity_match: anchor persist failed tenant=%s (non-terminal)", tenant_id)


def _seed_discovery(
    tenant_id: UUID | str, *, verified_name: str | None, gstin: str, seed: dict[str, Any]
) -> None:
    """Kick auto-discovery (async, NON-BLOCKING) seeded with the VERIFIED entity so discovery keys off
    the confirmed GSTIN/name, not the raw typed name (the Sundaram fix). Mirrors signup.py's kick;
    skipped cleanly outside a DBOS runtime (tests / non-workflow)."""
    try:
        from dbos import DBOS

        from orchestrator.onboarding.auto_discovery import auto_discovery_workflow

        merged = {**seed, "gstin": gstin}
        if verified_name:
            merged["business_name"] = verified_name  # discovery anchors on the VERIFIED name
        DBOS.start_workflow(auto_discovery_workflow, str(tenant_id), merged)
    except Exception:  # noqa: BLE001 — discovery is best-effort; never fail the confirm
        logger.exception("entity_match: discovery seed failed tenant=%s (non-terminal)", tenant_id)


def _default_search(query: str) -> list[dict[str, Any]]:
    import httpx

    token = os.environ.get(_TOKEN_ENV, "")
    resp = httpx.post(
        f"{_SEARCH_URL}?token={token}",
        json={"queries": query, "maxPagesPerQuery": 1, "resultsPerPage": 10, "countryCode": "in"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # The actor returns a list of SERP pages; flatten organicResults.
    out: list[dict[str, Any]] = []
    for page in data if isinstance(data, list) else []:
        for r in page.get("organicResults", []) or []:
            out.append(r)
    return out


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
