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
import os
import re
from pathlib import Path
from typing import Any, cast

import yaml
from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Data-entry methods the owner can record through (the rankable set).
CANDIDATE_METHODS = (
    "paper_book", "contacts", "upi", "kot_pos", "cash_book", "owner_typed",
)
# Scrape / context-enrichment methods — NEVER a first record-keeping suggestion.
EXCLUDED_METHODS = ("gbp", "swiggy", "zomato")

_MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
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


def _resolve_method_selector_model() -> str:
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["method_selector"][slot])


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
    input: MethodSelectorInput, *, client: Anthropic | None = None
) -> MethodSelectorOutput:
    """Rank the first record-keeping method to suggest. Validates the model's pick
    against CANDIDATE_METHODS (rejects any excluded/unknown method)."""
    if client is None:
        client = Anthropic()
    model = _resolve_method_selector_model()
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": input.business_context or "(no context provided)"}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
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
