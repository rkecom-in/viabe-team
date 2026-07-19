"""VT-677 — the CANONICAL owner-language module (Fazal rulings D1-D3, 2026-07-18).

ONE place for every owner-language decision. Before this, ~8 call sites hand-rolled
``COALESCE(preferred_language, language_preference, 'en')`` with a binary en|hi value space —
Hinglish (the register a large share of owners actually write) had NO stored representation, and
the two tenants columns had no defined semantics. This module fixes both:

COLUMN SEMANTICS (ratified design, no migration needed — both TEXT since migration 001):
  - ``tenants.preferred_language``  — the owner's EXPLICIT choice ("English only" / a settings
    change). NULL until a real choice is made. Written ONLY by :func:`set_explicit_language`.
  - ``tenants.language_preference`` — the OBSERVED rolling value (signup-toggle seed, then the
    per-turn triage inference). Written by :func:`record_observed_language`.
  - Read precedence (unchanged COALESCE order = explicit wins): preferred → observed → 'en'.

VALUE SPACE: ``en | hinglish | hi`` (``SUPPORTED_OWNER_LANGS``). 'hinglish' = Hindi in Latin
script (hi-Latn); 'hi' = Devanagari.

THE PRECEDENCE RULE (D2 — binding): live-turn MIRRORING is NEVER overridden. The stored preference
governs AGENT-INITIATED messages (welcome, nudges, stale-resume, monthly report, acks, template
variants) and AMBIGUOUS turns (emoji / one-word) only. Nothing in this module touches the
conversational brain's mirroring requirement.

TEMPLATE REGISTER (D1 — binding): a hinglish-preference owner gets the hi-Latn register on
free-form agent-initiated surfaces, and the EN template variant until Meta approves the hi-Latn
template variants — NEVER Devanagari for a hinglish-preference tenant. Pure 'hi' owners keep the
existing Devanagari templates. :func:`template_register` encodes exactly this.

CUSTOMER SENDS ARE OUT OF SCOPE (design e): campaign copy language is per-COHORT
(``CampaignPlan.message_plan.language``) and must NEVER be sourced from these columns — a
conformance test pins it.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

logger = logging.getLogger("orchestrator.owner_surface.owner_locale")

#: The full owner-language value space (VT-677). 'hinglish' = hi-Latn (romanized Hindi).
SUPPORTED_OWNER_LANGS = frozenset({"en", "hinglish", "hi"})

#: Devanagari block — the deterministic script override (a Devanagari turn IS 'hi'; no LLM needed).
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def is_devanagari(text: str) -> bool:
    """True iff ``text`` carries ANY Devanagari codepoint — the deterministic 'hi' override the
    triage inference applies before trusting the LLM's language enum (finite, script-level fact)."""
    return bool(_DEVANAGARI_RE.search(text or ""))


def resolve_owner_locale(tenant_id: UUID | str) -> str:
    """The owner's language for AGENT-INITIATED surfaces: explicit → observed → 'en'.

    Reads under ``tenant_connection`` (RLS is the isolation layer, VT-342 discipline). Best-effort:
    any error → 'en' (an agent-initiated message must still send). Values outside
    ``SUPPORTED_OWNER_LANGS`` normalize to 'en'.
    """
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT COALESCE(preferred_language, language_preference, 'en') AS lang "
                "FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:
        logger.exception("VT-677 locale resolve failed tenant=%s → en", tenant_id)
        return "en"
    lang = (dict(row).get("lang") if row else None) or "en"
    return lang if lang in SUPPORTED_OWNER_LANGS else "en"


def template_register(locale: str) -> str:
    """The Meta-TEMPLATE register for a resolved locale (D1):

      - 'hi'       → 'hi'  (existing Devanagari template variants)
      - 'hinglish' → 'en'  (EN fallback until the hi-Latn template variants are Meta-approved;
                            NEVER Devanagari for a hinglish-preference tenant)
      - 'en'/other → 'en'

    Free-form agent-initiated copy does NOT go through this — a hinglish owner gets the hi-Latn
    register directly there (no Meta constraint on in-session free-form).
    """
    return "hi" if locale == "hi" else "en"


def record_observed_language(tenant_id: UUID | str, lang: str) -> bool:
    """Persist the OBSERVED rolling language (``tenants.language_preference``) from the per-turn
    inference. Never touches ``preferred_language`` (D2: an off-register turn nudges the observed
    value only — it never rewrites an explicit choice). Best-effort False on error; ignores values
    outside the supported space (never write junk)."""
    if lang not in SUPPORTED_OWNER_LANGS:
        return False
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "UPDATE tenants SET language_preference = %s WHERE id = %s",
                (lang, str(tenant_id)),
            )
        return True
    except Exception:
        logger.exception("VT-677 observed-language persist failed tenant=%s", tenant_id)
        return False


def set_explicit_language(tenant_id: UUID | str, lang: str) -> bool:
    """Persist the owner's EXPLICIT choice (``tenants.preferred_language``) — the verbal-override
    path ("English only" → the set_language_preference tool). Explicit wins the read COALESCE from
    then on. Best-effort False on error; rejects values outside the supported space."""
    if lang not in SUPPORTED_OWNER_LANGS:
        return False
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "UPDATE tenants SET preferred_language = %s WHERE id = %s",
                (lang, str(tenant_id)),
            )
        return True
    except Exception:
        logger.exception("VT-677 explicit-language persist failed tenant=%s", tenant_id)
        return False


__all__ = [
    "SUPPORTED_OWNER_LANGS",
    "is_devanagari",
    "record_observed_language",
    "resolve_owner_locale",
    "set_explicit_language",
    "template_register",
]
