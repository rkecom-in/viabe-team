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
# VT-449 CIN: U/L + 5-digit industry + 2-letter state + 4-digit year + 3-letter type + 6-digit serial.
_CIN_RE = re.compile(r"\b[UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")

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
    source: str  # 'web' | 'gbp' | 'registry'
    candidate_gstin: str | None = None
    legal_name: str | None = None
    detail: str | None = None  # address/category — disambiguates "Sundaram"-class collisions
    candidate_cin: str | None = None  # VT-449: a registry CIN → MCA Company Master Data (validate/enrich)
    phone: str | None = None          # VT-411: the GBP public number → the ownership-OTP target


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
    candidates.extend(_cin_candidates(name, city, search_fn))  # VT-449 registry leg → CIN → MCA
    candidates.extend(_gbp_candidates(name, city, gbp_fetch_fn))
    # De-dup by (gstin or cin or trade_name); keep the first seen.
    seen: set[str] = set()
    out: list[EntityCandidate] = []
    for c in candidates:
        key = (c.candidate_gstin or c.candidate_cin or c.trade_name or "").upper()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


# Generic business-suffix/filler tokens that carry no distinctive identity — excluded when deciding
# whether a web result is ABOUT the queried business, so "RKeCom Services" doesn't match every
# "...Services"/"Telecom Services" GST page. VT-448 (the RKeCom discovery-noise fix).
_GENERIC_NAME_TOKENS = frozenset({
    "services", "service", "pvt", "ltd", "private", "limited", "the", "and", "co", "company",
    "llp", "opc", "inc", "enterprises", "enterprise", "solutions", "solution", "india", "indian",
    "store", "stores", "shop", "trading", "traders", "industries", "corporation", "group",
})


def _significant_tokens(name: str) -> set[str]:
    """The distinctive (non-generic, >=3-char) tokens of a business name — the identity signal a
    relevant web result must echo. Empty when a name is ALL generic (then we do NOT over-filter)."""
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3 and t not in _GENERIC_NAME_TOKENS}


def _result_is_relevant(blob: str, sig_tokens: set[str]) -> bool:
    """A web GST-search result is about the queried business only if its text echoes a distinctive
    name token. No distinctive token (all-generic name) → return True (don't drop real hits)."""
    if not sig_tokens:
        return True
    low = blob.lower()
    return any(t in low for t in sig_tokens)


def business_name_matches(typed: str | None, registry: str | None) -> bool:
    """VT-448 NAME-MATCH SECURITY: the Sandbox-authoritative registry name must plausibly be the owner's
    CLAIMED business — they must share a distinctive (non-generic) token. An unrelated-but-valid GSTIN
    (a DIFFERENT business's registration) therefore FAILS, so a valid GSTIN alone is not enough to earn a
    tenant. Lenient on suffix/word-order variation ("RKeCom Services Pvt Ltd" vs "RKECOM SERVICES (OPC)
    PRIVATE LIMITED" share 'rkecom'); strict on zero distinctive overlap. The caller collapses a mismatch
    into the SAME generic reject as invalid_gstin (no enumeration oracle — never "valid but not yours")."""
    t = _significant_tokens(typed or "")
    r = _significant_tokens(registry or "")
    if t and r:
        return bool(t & r)  # share ≥1 distinctive token
    # One side has no distinctive token (all-generic name) → normalized substring/equality fallback.
    tn = re.sub(r"[^a-z0-9]", "", (typed or "").lower())
    rn = re.sub(r"[^a-z0-9]", "", (registry or "").lower())
    return bool(tn) and bool(rn) and (tn in rn or rn in tn)


def enrich_company_from_cin(
    tenant_id: str, cin: str, *, reason: str, request_fn: Any = None
) -> str | None:
    """VT-449: fetch MCA Company Master Data by CIN → best-effort store (encrypted PII via mca_store) +
    return the canonical company name for PROFILE ENRICHMENT. NOTE: the GST create-gate name-match anchor
    stays the Sandbox ``verified_name`` (server-authoritative) — this MCA name is SUPPLEMENTARY enrichment,
    NEVER a substitute for the verify gate. None on any vendor/parse failure (never raises)."""
    from orchestrator.integrations.methods.mca import company_master_data

    cmd = company_master_data((cin or "").strip(), reason=reason, request_fn=request_fn)
    if not cmd.ok:
        return None
    try:
        from orchestrator.onboarding.mca_store import store_company_master_data

        store_company_master_data(tenant_id, cmd)
    except Exception:  # noqa: BLE001 — enrichment store is best-effort, never blocks identify
        logger.warning("entity_match: MCA company store failed (non-terminal)", exc_info=True)
    return cmd.company_name


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
    sig = _significant_tokens(name)
    out: list[EntityCandidate] = []
    for r in results or []:
        blob = " ".join(str(r.get(k, "")) for k in ("title", "description", "url", "text"))
        if not _result_is_relevant(blob, sig):
            continue  # VT-448: drop GST-SERP noise that doesn't name the queried business
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


def _cin_candidates(name: str, city: str, search_fn: SearchFn | None) -> list[EntityCandidate]:
    """VT-449 registry leg: a "<name> <city> CIN" SERP → MCA CIN candidate(s) (the input to Company
    Master Data). Relevance-filtered like the web leg; degrade to [] on absent token / failure."""
    token = os.environ.get(_TOKEN_ENV)
    fn = search_fn
    if fn is None:
        if not token:
            return []
        fn = _default_search
    try:
        results = fn(f"{name} {city} CIN".strip())
    except Exception:  # noqa: BLE001 — fragile web search; degrade, never raise into signup
        logger.warning("entity_match: CIN candidate search failed (degrade)", exc_info=True)
        return []
    sig = _significant_tokens(name)
    out: list[EntityCandidate] = []
    for r in results or []:
        blob = " ".join(str(r.get(k, "")) for k in ("title", "description", "url", "text"))
        if not _result_is_relevant(blob, sig):
            continue
        for cin in dict.fromkeys(_CIN_RE.findall(blob.upper())):  # ordered-unique
            out.append(
                EntityCandidate(
                    trade_name=_clean(r.get("title")),
                    source="registry",
                    candidate_cin=cin,
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
    sig = _significant_tokens(name)
    out: list[EntityCandidate] = []
    for place in (items or [])[:3]:
        title = _clean(place.get("title"))
        if not title:
            continue
        if not _result_is_relevant(title, sig):
            continue  # VT-448: drop a Maps result that doesn't name the queried business (fuzzy neighbour)
        loc = _clean(place.get("city")) or _clean(place.get("address"))
        cat = _clean(place.get("categoryName"))
        phone = _clean(place.get("phone") or place.get("phoneUnformatted"))  # VT-411 ownership-OTP target
        out.append(
            EntityCandidate(
                trade_name=title,
                source="gbp",
                detail=" · ".join(x for x in (cat, loc) if x) or None,
                phone=phone,
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


def persist_entity_anchor(
    tenant_id: UUID | str, *, gstin: str, verified_name: str | None,
    upsert_fn: Callable[[UUID | str, dict[str, Any]], Any] | None = None,
) -> None:
    """Public seam — persist the SERVER-VERIFIED entity anchor on a tenant that already exists.

    The verify-then-create completion (VT-406 reconciliation): run_signup verifies the GSTIN
    server-side (VT-408 gate) and creates the tenant, THEN calls this with the gate's authoritative
    gstin + name (NEVER a client-supplied value — IDOR). Best-effort, non-terminal."""
    _persist_anchor(tenant_id, gstin=gstin, verified_name=verified_name, upsert_fn=upsert_fn)


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
