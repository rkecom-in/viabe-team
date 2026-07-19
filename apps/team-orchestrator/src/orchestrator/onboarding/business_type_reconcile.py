"""VT-475 — business-TYPE reconciliation (the RKeCom mis-classification fix).

THE DEFECT (Fazal live-found): onboarding told RKeCom (rkecom.in — an e-commerce/software company)
"We found you're a **Telecommunications service provider** — is that right?". GBP had mis-categorized
the business and its raw ``categoryName`` flowed VERBATIM (auto_discovery_sources.discover_gbp →
draft ``category`` → question_brain._confirm_question "We found you're a {category}…"). No
reconciliation, no mapping, no cross-check — one mis-categorized public field surfaced unchecked.

THE FIX (the VT-452 LLM-discovery intent, applied to business-TYPE): GBP ``categoryName`` is ONE
signal, not gospel. ``reconcile_business_type`` weighs ALL available public signals — GBP category +
the domain/website (rkecom.in is plainly e-commerce) + GST nature-of-business (when verified) + the
company NAME + LLM web-knowledge — into a single Viabe-taxonomy ``business_type`` (config/
business_types.yaml) + a confidence + the signals used. When the raw GBP category CONFLICTS with the
domain/name, we do NOT lead with the wrong one — we prefer the reconciled type.

TAXONOMY: the COARSE ~19-bucket taxonomy in ``config/business_types.yaml`` (the same list signup's
``business_type`` field is constrained to — load-bearing for L3 cohorts / L4 skill targeting). Output
is always one of those ``key`` values, or ``other`` as the safe floor.

FAIL-SOFT (never crash onboarding): the LLM is OPTIONAL + injectable (``reconcile_fn`` seam) so this
is unit-testable without a key (the local key is dead — the live LLM path validates on dev). If the
LLM is absent or fails, we fall back to a DETERMINISTIC reconciliation (domain-keyword + name-keyword
+ GST-nature heuristics over the taxonomy) that STILL prefers a domain-derived guess over a
conflicting raw GBP category. The reconciler NEVER raises into discovery.

CONFIRM UX UNCHANGED: the reconciled type is still a HINT the owner confirms — question_brain keeps
the "is that right?" confirm step exactly; only the GUESS it shows changes to the reconciled one.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# VT-452 LLM pattern reused verbatim: the canonical bare model id (no date suffix) + the env the
# Anthropic SDK reads (ANTHROPIC_API_KEY, valid on deployed dev). web_search is optional — used only
# when the deterministic signals are thin (the company name carries no taxonomy keyword).
_RECONCILE_MODEL = "claude-opus-4-8"
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_RECONCILE_TIMEOUT_S = 25.0  # bound the call; a hang must degrade to the deterministic fallback

# (business_name, gbp_category, domain, gst_nature) -> the reconciled taxonomy key (a bare string).
# Injectable so the LLM leg is exercised without a real key / network in unit tests.
ReconcileFn = Callable[[str | None, str | None, str | None, str | None], str | None]


@dataclass(frozen=True)
class ReconciledType:
    """The reconciled business type + how confident we are + which signals fed it. ``business_type``
    is always a ``config/business_types.yaml`` key (``other`` is the safe floor)."""

    business_type: str
    confidence: str  # "high" | "medium" | "low"
    signals_used: list[str] = field(default_factory=list)
    raw_gbp_category: str | None = None


# --------------------------------------------------------------------------- taxonomy


@lru_cache(maxsize=1)
def _taxonomy() -> dict[str, str]:
    """Load config/business_types.yaml → {key: label_en}. Cached. The keys are the machine values
    stored on ``tenants.business_type``; ``other`` is always present as the floor. Fail-soft to a
    minimal in-code map if the file is unreadable (the reconciler must never crash onboarding)."""
    try:
        import pathlib

        import yaml

        # config/ is a sibling of src/ in apps/team-orchestrator. Resolve relative to THIS file
        # (…/src/orchestrator/onboarding/business_type_reconcile.py → …/config/business_types.yaml).
        cfg = pathlib.Path(__file__).resolve().parents[3] / "config" / "business_types.yaml"
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        out = {
            str(b["key"]): str(b.get("label_en") or b["key"])
            for b in data.get("business_types", [])
            if b.get("key")
        }
        if out:
            out.setdefault("other", "Other")
            return out
    except Exception:  # noqa: BLE001 — config read is best-effort; fall back to the in-code floor
        logger.warning(
            "business_type_reconcile: taxonomy load failed — in-code fallback", exc_info=True
        )
    return {"services": "Local services", "other": "Other"}


def is_valid_business_type(value: str | None) -> bool:
    """True iff ``value`` is a known taxonomy key (the LLM output is range-checked against this)."""
    return bool(value) and value in _taxonomy()


def taxonomy_keys() -> tuple[str, ...]:
    """The fixed taxonomy key set — for prompts that ask an LLM to pick the closest bucket
    (VT-568 website-derived type). The validator (`is_valid_business_type`) still gates every
    pick; this is prompt material, not a trust boundary."""
    return tuple(_taxonomy().keys())


# English → Hindi label map for the confirm UX (the taxonomy yaml carries label_hi; we cache the en
# map for is_valid_business_type and keep the hi map here for the bilingual confirm question).
@lru_cache(maxsize=1)
def _taxonomy_hi() -> dict[str, str]:
    try:
        import pathlib

        import yaml

        cfg = pathlib.Path(__file__).resolve().parents[3] / "config" / "business_types.yaml"
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        return {
            str(b["key"]): str(b.get("label_hi") or b.get("label_en") or b["key"])
            for b in data.get("business_types", [])
            if b.get("key")
        }
    except Exception:  # noqa: BLE001 — cosmetic; en label is the floor
        return {}


def taxonomy_label(key: str) -> tuple[str, str]:
    """The (label_en, label_hi) for a taxonomy ``key`` — for the onboarding confirm UX. Falls back to
    the key itself when the key is unknown / the config can't load (the confirm step still works)."""
    en = _taxonomy().get(key, key)
    hi = _taxonomy_hi().get(key, en)
    return en, hi


# Per-taxonomy-key keyword signals for the DETERMINISTIC fallback (no LLM). Matched against the
# domain host, the company name, and the GST nature-of-business text. COARSE on purpose — these are a
# floor for when the LLM is unavailable, not a precision classifier. Order matters only for the
# tie-break (first key with the most hits wins); ``other`` is the floor when nothing matches.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    # The RKeCom class: e-commerce / software / online retail-tech → 'services' (the closest coarse
    # bucket; there is no dedicated 'ecommerce'/'software' key — these route to local 'services').
    # DISTINCTIVE tokens only — 'service'/'services'/'store'/'online'/'retail' are deliberately
    # EXCLUDED (too generic: 'Telecommunications service provider' would spuriously match on 'service'
    # and the mis-category would falsely "agree", masking the conflict the domain is supposed to win).
    "services": (
        "ecom",
        "ecommerce",
        "commerce",
        "software",
        "saas",
        "shopify",
        "repair",
        "garment-tech",
    ),
    "kirana": ("kirana", "grocery", "general store", "provision", "supermarket"),
    "restaurant": ("restaurant", "dhaba", "diner", "eatery", "food"),
    "cafe_bakery": ("cafe", "coffee", "bakery", "bakers", "patisserie"),
    "sweets": ("sweet", "mithai", "halwai", "confection"),
    "salon_spa": ("salon", "spa", "beauty", "parlour", "parlor", "grooming"),
    "apparel": ("apparel", "boutique", "clothing", "garment", "fashion", "wear"),
    "pharmacy": ("pharmacy", "medical store", "chemist", "druggist", "medico"),
    "electronics": ("electronic", "mobile", "gadget", "appliance", "computer hardware"),
    "hardware": ("hardware", "building supplies", "sanitary", "paint", "cement"),
    "fitness": ("gym", "fitness", "crossfit", "yoga"),
    "education": ("coaching", "tuition", "academy", "institute", "education", "classes"),
    "healthcare": ("clinic", "hospital", "diagnostic", "dental", "physio", "healthcare"),
    "jewellery": ("jewel", "jewellery", "jewelry", "gold", "ornament"),
    "footwear": ("footwear", "shoe", "chappal", "sandal"),
    "dairy": ("dairy", "milk", "creamery"),
    "auto_garage": ("garage", "auto", "spares", "motor", "automobile", "tyre", "tire"),
    "book_stationery": ("book", "stationery", "stationary"),
}


# --------------------------------------------------------------------------- helpers


def _domain_of(website: str | None) -> str | None:
    """The registrable host of a website (lowercased, no scheme/www/path). 'https://rkecom.in/shop'
    → 'rkecom.in'. None when there's no parseable host (or it's a maps.google fallback url, which is
    NOT the business's own domain → carries no business-type signal)."""
    if not website:
        return None
    raw = website.strip()
    if not raw:
        return None
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
    if not host:
        return None
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    # A GBP maps URL is the LISTING, not the business's own site — no domain signal.
    if "google." in host or "goo.gl" in host:
        return None
    return host or None


def _coerce_nature(gst_nature: object) -> str | None:
    """Normalize a GST ``nature_of_business`` into a single keyword-matchable string.

    The GST verify (``sandbox_kyc.GstinLookup.nature_of_business``) is a ``list[str]`` of
    activities (e.g. ``['Supplier of Services', 'Others', 'Warehouse / Depot']``) — that real
    shape flows verbatim into the draft and on into ``reconcile_business_type`` via the VT-478
    recompose (``journey._draft_with_reconciled_type`` passes ``attrs['nature_of_business']``).
    The keyword matcher (``_best_keyword_key``) does ``t.lower()`` on each input and so raised
    ``AttributeError: 'list' object has no attribute 'lower'`` on a list — the reconcile then
    failed-soft to a no-op, silently re-surfacing the raw mis-categorized GBP ``category``
    (the "Telecommunications service provider?" confirm) instead of the reconciled type. Join
    the list into one space-separated blob so every list element's keywords are matched. A bare
    string passes through; anything else / empty → ``None`` (no signal)."""
    if gst_nature is None:
        return None
    if isinstance(gst_nature, str):
        return gst_nature or None
    if isinstance(gst_nature, (list, tuple)):
        joined = " ".join(str(part) for part in gst_nature if part)
        return joined or None
    # Any other scalar (defensive) — stringify; empty → None.
    text = str(gst_nature).strip()
    return text or None


def _gbp_category_to_key(gbp_category: str | None) -> str | None:
    """Map a raw GBP categoryName onto a taxonomy key — WORD-BOUNDARY matched (GBP categories are
    clean phrases: 'Sweet shop', 'Pharmacy'). So a SANE GBP category is honoured, while a mis-category
    ('Telecommunications service provider' — whose words hit no bucket; 'ecom' is buried INSIDE
    'telecommunications', not a whole word) yields None and is overridden by the domain/name. The
    word-boundary match is the RKeCom fix's lynchpin: a substring match would falsely read 'ecom' out
    of 'tel-ecom-munications' and let the mis-category masquerade as e-commerce."""
    return _best_keyword_key(gbp_category, word_boundary=True)


def _best_keyword_key(*texts: str | None, word_boundary: bool = False) -> str | None:
    """The taxonomy key whose keywords appear most across ``texts``; None when nothing matches.

    ``word_boundary=False`` (default, for DOMAIN/NAME signals): substring match — domains/names mash
    words together ('rkecom', 'acmepharmacy'), so the distinctive token must be findable mid-word.
    ``word_boundary=True`` (for the GBP CATEGORY): the keyword must appear on a word boundary — a clean
    GBP phrase shouldn't false-match a token buried inside an unrelated word ('ecom' in
    'telecommunications')."""
    blob = " ".join(t.lower() for t in texts if t)
    if not blob.strip():
        return None
    best_key, best_hits = None, 0
    for key, words in _KEYWORDS.items():
        if word_boundary:
            hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}", blob))
        else:
            hits = sum(1 for w in words if w in blob)
        if hits > best_hits:
            best_key, best_hits = key, hits
    return best_key


# --------------------------------------------------------------------------- deterministic core


def _deterministic_reconcile(
    business_name: str | None,
    gbp_category: str | None,
    domain: str | None,
    gst_nature: str | None,
) -> ReconciledType:
    """The no-LLM fallback. Cross-checks the signals over the taxonomy keyword table with this
    priority: a GBP category that maps to a SANE bucket and is NOT contradicted wins; otherwise the
    DOMAIN + NAME + GST signal dominates (the RKeCom fix — a mis-categorized GBP field must never beat
    a plain domain). ``other`` is the floor when nothing matches. Always returns — never raises."""
    signals: list[str] = []
    domain_key = _best_keyword_key(domain.replace(".", " ") if domain else None)
    name_key = _best_keyword_key(business_name)
    gst_key = _best_keyword_key(gst_nature)
    gbp_key = _gbp_category_to_key(gbp_category)

    if domain:
        signals.append("domain")
    if business_name:
        signals.append("name")
    if gst_nature:
        signals.append("gst_nature")
    if gbp_category:
        signals.append("gbp_category")

    # The domain/name/GST consensus (the business's OWN signals) is the trustworthy anchor.
    own_key = domain_key or gst_key or name_key

    # 1. GBP maps to a sane bucket AND agrees with the own-signal (or we have no own-signal) → high.
    if gbp_key and (own_key is None or own_key == gbp_key):
        return ReconciledType(
            gbp_key, "high" if own_key == gbp_key else "medium", signals, gbp_category
        )

    # 2. GBP maps to a sane bucket but the own-signal DISAGREES → the own-signal wins (RKeCom: GBP
    #    'Telecommunications' maps to nothing here, but even if a wrong GBP category DID map, a
    #    contradicting domain/name dominates). Conflict → medium confidence, never lead with GBP.
    if own_key and gbp_key and own_key != gbp_key:
        return ReconciledType(own_key, "medium", signals, gbp_category)

    # 3. GBP maps to NOTHING (the mis-category case) → use the own-signal if any.
    if own_key:
        return ReconciledType(own_key, "medium" if domain_key else "low", signals, gbp_category)

    # 4. No taxonomy signal anywhere → 'other' floor (the confirm step still lets the owner correct).
    return ReconciledType("other", "low", signals, gbp_category)


# --------------------------------------------------------------------------- public API


def reconcile_business_type(
    *,
    business_name: str | None = None,
    gbp_category: str | None = None,
    website: str | None = None,
    gst_nature: str | list[str] | None = None,
    reconcile_fn: ReconcileFn | None = None,
) -> ReconciledType:
    """Reconcile the available public signals into ONE Viabe-taxonomy ``business_type`` + confidence +
    the signals used. GBP ``categoryName`` is ONE signal, not gospel; a domain/name/GST signal that
    contradicts a mis-categorized GBP field WINS (the RKeCom fix — never lead with the wrong one).

    ``gst_nature`` accepts the real GST shape: a ``list[str]`` of activities (from
    ``sandbox_kyc.GstinLookup.nature_of_business``) OR a bare string. It is normalized to one
    keyword-matchable string at the boundary so a list never reaches the matcher (the silent-no-op
    bug: a list ``t.lower()`` raised, failing the recompose soft so the raw mis-categorized GBP
    category re-surfaced).

    The LLM leg (VT-452 pattern: lazy ``from anthropic import Anthropic`` + ``claude-opus-4-8``,
    optional web_search) is used when ENABLED + available; its output is RANGE-CHECKED against the
    taxonomy and discarded if out-of-range. ``reconcile_fn`` is the injectable seam (no key/network in
    tests). On ANY LLM failure / absence we fall back to ``_deterministic_reconcile`` — which still
    prefers the domain over a conflicting GBP category. NEVER raises (fail-soft into discovery)."""
    gst_nature = _coerce_nature(gst_nature)
    domain = _domain_of(website)
    deterministic = _deterministic_reconcile(business_name, gbp_category, domain, gst_nature)

    fn = reconcile_fn
    if fn is None and _llm_reconcile_enabled():
        fn = _default_llm_reconcile
    if fn is None:
        return deterministic  # no LLM configured → the deterministic cross-check stands

    try:
        key = fn(business_name, gbp_category, domain, gst_nature)
    except Exception:  # noqa: BLE001 — LLM/parse fragile; degrade to the deterministic reconcile
        logger.warning(
            "business_type_reconcile: LLM leg failed — deterministic fallback", exc_info=True
        )
        return deterministic

    if is_valid_business_type(key):
        signals = sorted(set(deterministic.signals_used) | {"llm"})
        return ReconciledType(str(key), "high", signals, gbp_category)
    # LLM returned junk / out-of-taxonomy → trust the deterministic cross-check, not the raw GBP field.
    logger.info("business_type_reconcile: LLM key %r not in taxonomy — deterministic fallback", key)
    return deterministic


def _llm_reconcile_enabled() -> bool:
    """The LLM reconcile leg is gated behind the SAME ``ENABLE_LLM_DISCOVERY`` flag as the VT-452
    entity-discovery leg (one switch for the LLM-discovery family). Default OFF; the deterministic
    cross-check (which alone already fixes the RKeCom case) runs regardless."""
    return os.environ.get("ENABLE_LLM_DISCOVERY", "").strip().lower() in {"1", "true", "yes", "on"}


def _default_llm_reconcile(
    business_name: str | None, gbp_category: str | None, domain: str | None, gst_nature: str | None
) -> str | None:
    """VT-452 LLM pattern: ask ``claude-opus-4-8`` (optional server-side web_search when the signals
    are thin) to reconcile the public signals into ONE taxonomy key. REUSES the lazy ``from anthropic
    import Anthropic`` SDK (no new client); ANTHROPIC_API_KEY is read from env. Returns the bare key
    string (the caller range-checks it) or None. Raises on SDK/parse failure → the caller degrades."""
    from anthropic import Anthropic

    keys = _taxonomy()
    catalogue = "\n".join(f"  {k}: {label}" for k, label in keys.items())
    # web_search helps only when the deterministic signals are thin — the name carries no keyword and
    # there's no domain (a bare GST/GBP-mislabelled business). Cheap to always offer; the model elects.
    thin = not _best_keyword_key(domain, business_name, gst_nature)
    prompt = (
        "Classify an Indian small business into EXACTLY ONE category from this fixed taxonomy "
        "(reply with the bare key only, lowercase, nothing else):\n"
        f"{catalogue}\n\n"
        "Signals (each is a HINT, not gospel — the Google Business 'category' is OFTEN WRONG; when it "
        "conflicts with the website domain or the company name, TRUST THE DOMAIN/NAME, not the GBP "
        "category):\n"
        f"  company_name: {business_name or 'unknown'}\n"
        f"  website_domain: {domain or 'none'}\n"
        f"  google_business_category: {gbp_category or 'none'}\n"
        f"  gst_nature_of_business: {gst_nature or 'none'}\n\n"
        "Example: name 'RKeCom Services', domain 'rkecom.in', GBP category 'Telecommunications service "
        "provider' → the domain+name say e-commerce/software, so the GBP 'Telecommunications' is a "
        "mis-category; pick the e-commerce/software/online-retail bucket, NOT a telecom one.\n"
        "Reply with ONE key from the taxonomy above."
    )
    kwargs: dict[str, Any] = {
        "model": _RECONCILE_MODEL,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": prompt}],
        "timeout": _RECONCILE_TIMEOUT_S,
    }
    if thin:
        kwargs["tools"] = [_WEB_SEARCH_TOOL]
        kwargs["max_tokens"] = 512  # web_search loop needs room; we still parse only the final key
    resp = Anthropic().messages.create(**kwargs)
    parts = [
        getattr(block, "text", "")
        for block in (resp.content or [])
        if getattr(block, "type", None) == "text"
    ]
    text = " ".join(p for p in parts if p)
    # Extract the first taxonomy key token the model emitted (range-checked by the caller).
    for token in re.findall(r"[a-z_]+", text.lower()):
        if token in keys:
            return token
    return text.strip().lower() or None
