"""VT-568 — entity resolution for auto-discovery (the RKeCom "wrong company" fix).

THE DEFECT (Fazal live drill): ``discover_gbp`` queried ``"{business_name} {city}"``, took
``items[0]`` BLIND, and chained that hit's website/category/about into the DRAFT. For the drill
tenant ("RKECOM Services PVT LTD", Mumbai) Google's Maps panel returned **Reecomps teleservices
pvt ltd** — a phonetic near-miss, a DIFFERENT company — whose reecomps.in site, "Telecommunications
service provider" category, and about-text polluted the draft. Meanwhile the GST leg (verified) held
the CORRECT entity (RKECOM SERVICES (OPC) PRIVATE LIMITED, Santacruz West Mumbai) and Google's own
organic results pointed at the real rkecom.in. Nothing cross-checked anything.

THE FIX — adjudicate the GBP candidates against the owner's OWN authoritative anchors BEFORE any
GBP-derived field enters the draft:

  1. DETERMINISTIC FLOOR (cheap, always runs, no LLM): normalized name-token similarity between a
     candidate's title and the owner anchors (signup name + GST legal/trade name), via the SAME
     ``entity_match.business_name_matches`` normalizer the confirm-seam uses. "RKECOM" vs "Reecomps"
     shares no distinctive token → FAILS. Plus a locality check when the GST principal address gives a
     city: a candidate in a clearly-different city is a strong reject. The floor is a HARD gate — the
     LLM can never resurrect a candidate the floor killed.
  2. LLM ADJUDICATOR (ONE ``claude-opus-4-8`` call + server-side web_search — the house idiom shared
     with ``entity_match``/``business_type_reconcile``): REASONS about which candidate, if any, IS the
     owner's company (phonetic lookalikes are the known trap; the owner's own domain + GST legal name
     dominate over a Google 'category'), and can resolve the owner's real website from organic evidence
     when GBP is rejected. Structured, validated JSON out.
  3. DECISION (fail-closed on identity): floor-pass AND LLM high/medium agreement → ACCEPT the
     candidate (+ its website). Anything else — no agreement, LLM error/timeout, no candidate — →
     REJECT ALL GBP candidates: no category, no website chain, no about from a maybe-wrong listing.
     GST facts always stand. If the LLM confidently resolves the owner's OWN website from organic
     evidence (its domain plausibly matches the name anchors), that website may still seed the website
     source against the RIGHT site.

Fail-soft: any adjudicator error/timeout → REJECT (fail-closed on identity, never crash discovery).
PII (CL-390/425, DPDP): only business-level identity — signup name, GST trade/legal name (the GST leg
already excludes a proprietor's personal ``lgnm``), locality, category, website — is reasoned over;
never a natural person's name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# VT-452/VT-475 house LLM idiom, reused verbatim: the capable reasoning model (identity resolution IS
# the reasoning-critical step — Fazal: "the LLM needs to reason correctly") + the current dynamic-
# filtering web_search tool for the Opus-4.x family. ANTHROPIC_API_KEY is read from env (valid on
# deployed dev/prod; the local key is dead — this validates on dev per CL-2026-06-29).
_ADJUDICATOR_MODEL = "claude-opus-4-8"
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
_ADJUDICATOR_MAX_TOKENS = 900  # room for the web_search loop; we parse only the final JSON block
_ADJUDICATOR_TIMEOUT_S = 30.0  # a hang MUST degrade to fail-closed reject, never wedge discovery
_MAX_REASONING_CHARS = 600  # bound the reasoning we persist into provenance

# Per-run adjudication cost estimate (USD) — one Opus call + up to 3 bounded web searches. Fixed
# estimate for the engine's cost ceiling + the observability spend record (the SDK returns no $).
ADJUDICATION_COST_USD = 0.025

# (owner_anchors, candidates) -> {matched_candidate_index, resolved_website, confidence, reasoning}.
# Injectable so the LLM leg is exercised without a real key / network in unit tests.
AdjudicateFn = Callable[["OwnerAnchors", "list[GbpCandidate]"], "dict[str, Any] | None"]


@dataclass(frozen=True)
class OwnerAnchors:
    """The owner's OWN authoritative identity signals — what a GBP candidate must plausibly BE.
    ``signup_name`` is the (server-verified, VT-406) business name; the GST fields are the Sandbox-
    verified entity (business-level only — a proprietor's personal ``lgnm`` is never included here,
    the GST leg gates it out). ``owner_website`` is an owner-provided site if any."""

    signup_name: str | None
    gst_legal_name: str | None = None
    gst_trade_name: str | None = None
    gst_principal_address: str | None = None
    owner_website: str | None = None
    city: str | None = None

    def names(self) -> list[str]:
        """The distinctive name anchors a candidate title / a resolved domain must echo."""
        return [n for n in (self.signup_name, self.gst_legal_name, self.gst_trade_name) if n]


@dataclass(frozen=True)
class GbpCandidate:
    """One GBP Maps hit under adjudication. ``index`` is its position in the fetched top-N list —
    it is what the LLM references as ``matched_candidate_index``."""

    index: int
    title: str | None
    category: str | None = None
    address: str | None = None
    city: str | None = None
    website: str | None = None


@dataclass(frozen=True)
class ResolutionResult:
    """The adjudication outcome. ``decision`` gates whether ANY GBP field may enter the draft.
    ``resolved_website`` is the accepted candidate's site OR — even on reject — an organic-resolved
    OWNER site (plausible against the name anchors) that the website source may still run against."""

    decision: str  # "accept" | "reject"
    matched_index: int | None
    resolved_website: str | None
    confidence: str  # "high" | "medium" | "low"
    reasoning: str
    rejected_titles: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- deterministic floor


# Address filler that carries no locality identity — excluded so a shared "road"/"floor" doesn't read
# as a shared city. Kept small + coarse (the floor is a cheap gate, not a geocoder).
_ADDR_STOPWORDS = frozenset({
    "road", "street", "lane", "cross", "main", "near", "opp", "opposite", "behind", "floor", "flno",
    "building", "plot", "shop", "gala", "wing", "block", "sector", "phase", "nagar", "marg", "chowk",
    "india", "state", "district", "west", "east", "north", "south", "central", "new", "old",
})


def _locality_tokens(text: str | None) -> set[str]:
    """Coarse locality tokens (lowercased alnum, >=4 chars, non-filler) from an address / city string.
    Used only to REJECT on a clear city mismatch — never to assert a match."""
    if not text:
        return set()
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 4 and t not in _ADDR_STOPWORDS}


def _locality_ok(owner_loc: set[str], candidate_loc: set[str]) -> bool:
    """Locality gate: only a REJECT signal. When BOTH sides carry locality tokens and share NONE, the
    candidate is in a different place → reject. Otherwise (either side empty) don't gate on locality —
    same city ≠ same company, but the name floor governs; a missing address must not over-reject."""
    if not owner_loc or not candidate_loc:
        return True
    return bool(owner_loc & candidate_loc)


def _name_floor_pass(candidate_title: str | None, anchor_names: list[str]) -> bool:
    """The candidate title must share a distinctive (non-legal-suffix) token with an owner name anchor.
    Reuses ``entity_match.business_name_matches`` — the SAME VT-448/510 normalizer (strips OPC/PVT/LTD,
    handles the OPC expansion, casefolds) the GST confirm-seam uses — so "RKECOM" vs "Reecomps" FAILS
    while "RKeCom Services" vs "RKECOM SERVICES (OPC) PRIVATE LIMITED" passes. Lazy import: entity_match
    is stdlib-only at module load (dep-less-smoke safe)."""
    if not candidate_title or not anchor_names:
        return False
    from orchestrator.onboarding.entity_match import business_name_matches

    return any(business_name_matches(a, candidate_title) for a in anchor_names)


def _floor_pass(candidate: GbpCandidate, anchors: OwnerAnchors, owner_loc: set[str]) -> bool:
    """A candidate clears the deterministic floor iff its title shares a distinctive name token with an
    owner anchor AND it is not in a clearly-different locality. Hard gate — the LLM cannot override it."""
    if not _name_floor_pass(candidate.title, anchors.names()):
        return False
    cand_loc = _locality_tokens(candidate.address) | _locality_tokens(candidate.city)
    return _locality_ok(owner_loc, cand_loc)


# --------------------------------------------------------------------------- website plausibility


def _domain_label(url: str | None) -> str | None:
    """The registrable label of a URL host — 'https://www.rkecom.in/shop' → 'rkecom'. None when there's
    no parseable host or it's a maps.google fallback (the LISTING url, not the business's own domain)."""
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
    if not host:
        return None
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if "google." in host or "goo.gl" in host:
        return None
    label = host.split(".", 1)[0]
    return label or None


def _website_plausible(url: str | None, anchor_names: list[str]) -> bool:
    """A resolved website is trustworthy only if its domain label plausibly matches an owner NAME anchor
    — the guard that stops the LLM from injecting an arbitrary/wrong domain (e.g. a hallucinated
    reecomps.in would fail against {rkecom}). Reuses the same name-match normalizer as the floor."""
    label = _domain_label(url)
    if not label or not anchor_names:
        return False
    from orchestrator.onboarding.entity_match import business_name_matches

    return any(business_name_matches(a, label) for a in anchor_names)


def _clean_url(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


# --------------------------------------------------------------------------- public API


def resolve_entity(
    anchors: OwnerAnchors,
    candidates: list[GbpCandidate],
    *,
    adjudicate_fn: AdjudicateFn | None = None,
) -> ResolutionResult:
    """Adjudicate GBP ``candidates`` against the owner ``anchors``. Deterministic floor + one LLM
    adjudication → a fail-closed accept/reject decision (see the module docstring). NEVER raises."""
    if not candidates:
        return ResolutionResult("reject", None, None, "low", "no GBP candidates to adjudicate", [])

    anchor_names = anchors.names()
    owner_loc = _locality_tokens(anchors.gst_principal_address) | _locality_tokens(anchors.city)
    floor_ok = {c.index: _floor_pass(c, anchors, owner_loc) for c in candidates}
    all_titles = [c.title for c in candidates if c.title]

    fn = adjudicate_fn or _default_adjudicate
    try:
        verdict = fn(anchors, candidates)
    except Exception:  # noqa: BLE001 — adjudicator fragile (LLM/network/parse); fail-closed, never crash
        logger.warning("entity_resolution: adjudicator raised — fail-closed reject-all-GBP", exc_info=True)
        verdict = None

    if not verdict:
        return ResolutionResult(
            "reject", None, None, "low",
            "adjudicator unavailable — fail-closed reject-all-GBP", all_titles,
        )

    idx = verdict.get("matched_candidate_index")
    if not isinstance(idx, int) or isinstance(idx, bool):
        idx = None
    conf = verdict.get("confidence")
    if conf not in ("high", "medium", "low"):
        conf = "low"
    reasoning = str(verdict.get("reasoning") or "").strip()[:_MAX_REASONING_CHARS]
    resolved_website = _clean_url(verdict.get("resolved_website"))

    # ACCEPT only when the LLM picked a candidate at high/medium AND that candidate cleared the
    # deterministic floor (defense-in-depth: the LLM cannot resurrect a floor-failing lookalike).
    if idx is not None and 0 <= idx < len(candidates) and conf in ("high", "medium") and floor_ok.get(idx):
        accepted = candidates[idx]
        website = accepted.website or (
            resolved_website if _website_plausible(resolved_website, anchor_names) else None
        )
        return ResolutionResult(
            "accept", accepted.index, website, conf, reasoning,
            rejected_titles=[c.title for c in candidates if c.index != accepted.index and c.title],
        )

    # REJECT ALL GBP candidates — but still surface an organic-resolved OWNER website if the LLM found
    # one whose domain plausibly matches the name anchors (the drill: reject Reecomps, still find rkecom.in).
    website = resolved_website if _website_plausible(resolved_website, anchor_names) else None
    return ResolutionResult("reject", None, website, conf, reasoning, rejected_titles=all_titles)


def _default_adjudicate(anchors: OwnerAnchors, candidates: list[GbpCandidate]) -> dict[str, Any] | None:
    """The real LLM leg: ONE ``claude-opus-4-8`` call with the server-side web_search tool. Frames the
    task as REASONING ("which of these, if any, IS the owner's company"), never a scripted checklist,
    and returns strict JSON. Lazy ``from anthropic import Anthropic`` (VT-452 pattern); raises on any
    SDK/parse failure → ``resolve_entity`` degrades it to a fail-closed reject."""
    from anthropic import Anthropic

    lines = []
    for c in candidates:
        lines.append(
            f"  [{c.index}] title={c.title!r} category={c.category!r} "
            f"address={c.address!r} website={c.website!r}"
        )
    candidate_block = "\n".join(lines) if lines else "  (none)"
    prompt = (
        "You are identifying which Google Business listing, if any, IS a specific owner's OWN "
        "business — so an onboarding agent doesn't attach the wrong company's website, category, and "
        "description to their profile.\n\n"
        "THE OWNER (authoritative signals):\n"
        f"  signup_name: {anchors.signup_name or 'unknown'}\n"
        f"  gst_legal_name: {anchors.gst_legal_name or 'none'}\n"
        f"  gst_trade_name: {anchors.gst_trade_name or 'none'}\n"
        f"  gst_principal_address: {anchors.gst_principal_address or 'none'}\n"
        f"  owner_provided_website: {anchors.owner_website or 'none'}\n\n"
        "GOOGLE BUSINESS CANDIDATES (each a HINT, none trusted):\n"
        f"{candidate_block}\n\n"
        "Reason about which candidate, if any, is genuinely the owner's company. The KNOWN TRAP is a "
        "PHONETIC LOOKALIKE — a different company whose name merely sounds like the owner's (e.g. "
        "'Reecomps' is NOT 'RKECOM'). The owner's OWN registered name (GST legal/trade name) and their "
        "OWN website domain dominate; a Google 'category' is often wrong and must never outweigh the "
        "name/domain. Use web search to find the owner's real company and its own website (their own "
        "domain is usually the top organic result). If NONE of the candidates is the owner's company, "
        "return matched_candidate_index=null. If you can identify the owner's OWN website from search "
        "(its domain plausibly matches the owner's name), return it as resolved_website even when no "
        "candidate matches.\n\n"
        "Reply with ONLY this JSON (no prose, no markdown):\n"
        '{"matched_candidate_index": <int or null>, "resolved_website": <string or null>, '
        '"confidence": "high"|"medium"|"low", "reasoning": "<one or two sentences>"}'
    )
    resp = Anthropic().messages.create(
        model=_ADJUDICATOR_MODEL,
        max_tokens=_ADJUDICATOR_MAX_TOKENS,
        system=(
            "You are a careful business-identity resolver. You reason about company identity from name "
            "and domain evidence, treat phonetic lookalikes as distinct companies, and reply with ONLY "
            "valid JSON parseable by json.loads()."
        ),
        tools=[_WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": prompt}],
        timeout=_ADJUDICATOR_TIMEOUT_S,
    )
    # The LAST text block is the model's final answer after web_search completes (earlier text blocks
    # may be "I'll search…" preamble) — the same house parse as entity_match._default_llm_search.
    parts = [
        getattr(block, "text", "")
        for block in (resp.content or [])
        if getattr(block, "type", None) == "text"
    ]
    return _parse_verdict((parts[-1] if parts else "").strip())


def _parse_verdict(raw: str) -> dict[str, Any] | None:
    """Strict-ish JSON parse of the adjudicator answer. A non-JSON/empty response → None (fail-closed).
    Tolerates a short sentence before the object (web_search flows sometimes prepend one)."""
    import json

    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None
