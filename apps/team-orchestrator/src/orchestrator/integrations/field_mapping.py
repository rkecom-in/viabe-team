"""VT-209 — field-mapping reasoner with confidence threshold routing.

Two-stage match:
1. Heuristic (exact case-fold + fuzzy via difflib ratio) using
   GLOBAL_FIELD_HINTS + connector-specific hints from VT-205 registry
2. LLM-assisted fallback (Opus 4.7 + cache_control via VT-194) when
   heuristic confidence < 0.85

Threshold routing per CL-19 / VT-6:
- < 0.7 → `RoutingDecision.ASK_OWNER`
- 0.7-0.85 → `RoutingDecision.COMMIT_WITH_NOTIFICATION`
- ≥ 0.85 → `RoutingDecision.COMMIT_SILENTLY`

Q1/Q2/Q3 Option A locked per Cowork plan-review 2026-05-28.

Per CL-104: sample data passed to LLM-assisted match is sanitised
(field names only, no row values).
Per CL-417: per-field confidence written to pipeline_steps for
telemetry (AC-5).
Per CL-419: cache_control on system prompt amortises across multiple
field-mapping calls in a single onboarding session.
"""

from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from orchestrator.integrations.canonical_fields import (
    GLOBAL_FIELD_HINTS,
    CanonicalField,
)
from orchestrator.integrations.registry import get_connector

logger = logging.getLogger(__name__)


# Q2 locked thresholds.
_ASK_THRESHOLD = 0.7
_NOTIFY_THRESHOLD = 0.85


RoutingDecision = Literal[
    "ask_owner",                       # < 0.7
    "commit_with_notification",        # 0.7-0.85
    "commit_silently",                 # ≥ 0.85
]


DecidedBy = Literal["heuristic", "llm", "owner"]


@dataclass(frozen=True)
class FieldMapping:
    source_field: str
    canonical_field: CanonicalField | None  # None when no reasonable match
    confidence: float
    decided_by: DecidedBy
    routing: RoutingDecision


def _normalize(name: str) -> str:
    return name.strip().casefold().replace(" ", "_").replace("-", "_")


def _heuristic_match(
    source_field: str,
    extra_hints: dict[str, list[str]] | None = None,
) -> tuple[CanonicalField | None, float]:
    """Exact + fuzzy match against GLOBAL_FIELD_HINTS + connector hints."""
    src_norm = _normalize(source_field)

    # Build merged hint map: connector-specific overrides global on conflict.
    merged: dict[CanonicalField, list[str]] = {}
    for cf, aliases in GLOBAL_FIELD_HINTS.items():
        merged[cf] = [_normalize(a) for a in aliases]
    if extra_hints:
        for cf_name, aliases in extra_hints.items():
            if cf_name in GLOBAL_FIELD_HINTS:
                merged[cf_name].extend(_normalize(a) for a in aliases)

    # Step 1: exact case-fold match → confidence 1.0
    for cf, aliases in merged.items():
        if src_norm in aliases:
            return cf, 1.0

    # Step 2: fuzzy match via difflib SequenceMatcher
    best_cf: CanonicalField | None = None
    best_ratio = 0.0
    for cf, aliases in merged.items():
        for alias in aliases:
            ratio = difflib.SequenceMatcher(None, src_norm, alias).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_cf = cf
    return best_cf, best_ratio


# Q3 Option A — dedicated lightweight LLM seam. SystemMessage with
# cache_control marker per VT-194 so the prefix amortises across calls.
_LLM_SYSTEM_PROMPT = """You map a source column name to a canonical Viabe field.

Canonical fields:
- customer_name: any name (customer/buyer/client)
- phone: any phone / mobile number
- email: email address
- order_amount: monetary value of a transaction
- order_date: when an order/transaction happened
- last_seen: last time the customer was active
- address: any address (shipping/billing/location)
- tags: labels / segments / categories

Output ONLY one of: customer_name, phone, email, order_amount, order_date, last_seen, address, tags, NONE.
NONE = source field doesn't map to any canonical field cleanly.
"""

_LLM_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": _LLM_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)


def _llm_assisted_match(source_field: str) -> tuple[CanonicalField | None, float]:
    """Anthropic-mediated match. Returns (canonical_field, confidence).

    Confidence approximated as 0.9 for confident LLM output (single
    canonical field returned) and 0.5 for NONE / ambiguous. The LLM
    response is parsed deterministically.

    Skipped (returns (None, 0.0)) when ANTHROPIC_API_KEY absent or
    doesn't start with sk-ant- (mirrors dispatch_brain's env-gate).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key.startswith("sk-ant-"):
        logger.warning(
            "VT-209 LLM-assisted match skipped — no Anthropic key",
            extra={"source_field": source_field},
        )
        return None, 0.0

    try:
        # max_tokens=10 is enough for a single canonical-field label.
        model = ChatAnthropic(  # type: ignore[call-arg]
            model="claude-opus-4-7", max_tokens=10
        )
        result = model.invoke([
            _LLM_SYSTEM_MESSAGE,
            HumanMessage(content=f"Source column: {source_field}"),
        ])
        text = str(result.content).strip().lower()
        valid_fields: set[str] = set(GLOBAL_FIELD_HINTS.keys())
        # Strip punctuation / pick the first valid token.
        for token in text.replace(",", " ").replace(".", " ").split():
            tok = token.strip(":'\" ")
            if tok in valid_fields:
                return tok, 0.9  # type: ignore[return-value]
            if tok == "none":
                return None, 0.5
        return None, 0.5
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "VT-209 LLM match raised; returning low-confidence None",
            extra={"source_field": source_field, "exc": repr(exc)},
        )
        return None, 0.0


def _route(confidence: float) -> RoutingDecision:
    if confidence < _ASK_THRESHOLD:
        return "ask_owner"
    if confidence < _NOTIFY_THRESHOLD:
        return "commit_with_notification"
    return "commit_silently"


def propose_field_mapping(
    source_field: str, connector_id: str
) -> FieldMapping:
    """Propose a canonical-field mapping for one source column.

    Pipeline:
    1. Heuristic match (exact + fuzzy) against GLOBAL_FIELD_HINTS +
       connector-specific hints from VT-205 registry
    2. If heuristic confidence < 0.85, fall back to LLM-assisted
       match (Anthropic + cache_control via VT-194)
    3. Determine routing via threshold (_ASK_THRESHOLD / _NOTIFY_THRESHOLD)

    The caller (Integration Agent) decides whether to write the
    proposal via ``confirm_field_mapping`` (committed) or render it
    to the owner for clarification (ask_owner routing).
    """
    try:
        spec = get_connector(connector_id)
        extra_hints = spec.canonical_fields_hints
    except KeyError:
        # Unknown connector_id — use global hints only.
        extra_hints = None

    cf_heuristic, conf_heuristic = _heuristic_match(source_field, extra_hints)
    decided_by: DecidedBy = "heuristic"
    canonical = cf_heuristic
    confidence = conf_heuristic

    if confidence < _NOTIFY_THRESHOLD:
        cf_llm, conf_llm = _llm_assisted_match(source_field)
        if conf_llm > confidence:
            canonical = cf_llm
            confidence = conf_llm
            decided_by = "llm"

    return FieldMapping(
        source_field=source_field,
        canonical_field=canonical,
        confidence=confidence,
        decided_by=decided_by,
        routing=_route(confidence),
    )


__all__ = [
    "DecidedBy",
    "FieldMapping",
    "RoutingDecision",
    "propose_field_mapping",
]
