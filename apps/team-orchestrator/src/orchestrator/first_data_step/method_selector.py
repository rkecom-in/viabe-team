"""VT-267 PR-B — method_selector.

Haiku-backed ranker: given an owner's business context, ranks which record-keeping
method to suggest FIRST during onboarding. Candidate set is the DATA-entry methods only
{paper_book, contacts, upi, kot_pos, cash_book, owner_typed}; the SCRAPE methods
{gbp, swiggy, zomato} are EXCLUDED (they're context-enrichment, not owner record-keeping).

Classification task (pick from a fixed set given context), not open-ended reasoning →
Haiku both slots (same posture as owner_input_classifier / owner_typed_extraction).
Pure LLM call: no DB read — the caller passes tenant context. Model pin from
config/models.yaml (Pillar 8 — never hardcode). JSON-only output.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Data-entry methods the owner can record through (the rankable set).
CANDIDATE_METHODS = (
    "paper_book", "contacts", "upi", "kot_pos", "cash_book", "owner_typed",
)
# Scrape / context-enrichment methods — NEVER a first record-keeping suggestion.
EXCLUDED_METHODS = ("gbp", "swiggy", "zomato")

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)

Method = str


class MethodSelectorInput(BaseModel):
    """Tenant context for the ranking. business_context is free-form (business type,
    whether they have a POS, take UPI, keep a paper book, etc.) — the caller assembles
    it from the business profile; this tool does NOT read the DB."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    business_context: str = Field(default="", max_length=4000)


class MethodSelectorOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommended_method: Method
    confidence: float = Field(..., ge=0.0, le=1.0)
    alternatives: list[Method] = Field(default_factory=list)


# VT-732 — the CLASSIFIER tier (TEAM_MODEL_CLASSIFIER), replacing the
# ``config/models.yaml[method_selector][VIABE_ENV-slot]`` pin: picking one method from a fixed
# candidate set is classification, and the env var already expresses the per-environment split the
# yaml encoded.
_SELECTOR_TIER = "classifier"


def _resolve_method_selector_model() -> str:
    """The concrete model the selector tier resolves to — for logs/attribution only."""
    from orchestrator.llm import resolve_model_id

    return resolve_model_id(_SELECTOR_TIER)


_SYSTEM_PROMPT = f"""\
You rank record-keeping methods for a small Indian business owner who is starting to
record their business data in the Viabe Team system.

Pick the SINGLE best method to suggest FIRST, plus ranked alternatives, from EXACTLY
this candidate set (and NOTHING else):
  {", ".join(CANDIDATE_METHODS)}

NEVER output any of these (they are scrape/context methods, not owner record-keeping):
  {", ".join(EXCLUDED_METHODS)}

Method meanings:
- owner_typed: owner just types entries in WhatsApp (lowest friction; default when unsure)
- contacts: import the phone contact list
- upi: upload/forward a UPI transaction export
- paper_book: photograph a handwritten ledger
- cash_book: photo/voice of a cash book
- kot_pos: connect/export from a POS / KOT system (only if they clearly have one)

Strategy: prefer the LOWEST-friction method that fits the owner's context. If a POS is
mentioned use kot_pos; if they mention UPI heavily use upi; if they keep a paper ledger
use paper_book/cash_book; otherwise default to owner_typed or contacts.

Output a single JSON object with EXACTLY these fields and nothing else:
  recommended_method: one of the candidate methods
  confidence: float in [0.0, 1.0]
  alternatives: ordered list of other candidate methods (most-to-least suitable)

JSON only. No markdown fences. No prose.
"""


def rank_method(
    input: MethodSelectorInput, *, text_call: Callable[..., str] | None = None
) -> MethodSelectorOutput:
    """Rank the first record-keeping method to suggest. Validates the model's pick
    against CANDIDATE_METHODS (rejects any excluded/unknown method).

    VT-732 — ``text_call`` replaces the injectable Anthropic ``client``; the transport is the
    multi-provider seam."""
    from orchestrator.llm.structured import structured_text_call

    _call = text_call or structured_text_call
    raw = _call(
        _SELECTOR_TIER,
        system=_SYSTEM_PROMPT,
        user=input.business_context or "(no context provided)",
        max_tokens=200,
        agent="first_data_step",
        call_site="method_selector",
    ).strip()
    if not raw:
        raise ValueError("method_selector: model returned empty content")
    m = _CODE_FENCE_RE.match(raw)
    if m:
        raw = m.group("body").strip()
    try:
        parsed: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"method_selector: non-JSON: {raw[:200]!r}") from exc
    out = MethodSelectorOutput(**parsed)
    if out.recommended_method not in CANDIDATE_METHODS:
        raise ValueError(
            f"method_selector: recommended '{out.recommended_method}' not in candidates "
            f"(excluded scrape method or unknown)"
        )
    # drop any non-candidate alternatives defensively (model must never surface scrape).
    bad = [a for a in out.alternatives if a not in CANDIDATE_METHODS]
    if bad:
        out = out.model_copy(
            update={"alternatives": [a for a in out.alternatives if a in CANDIDATE_METHODS]}
        )
    return out


__all__ = [
    "CANDIDATE_METHODS",
    "EXCLUDED_METHODS",
    "MethodSelectorInput",
    "MethodSelectorOutput",
    "rank_method",
]
