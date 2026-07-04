"""VT-49 — classify_owner_message standalone tool.

Haiku-backed classifier. Takes a free-text owner message; emits a
typed envelope with classification + confidence + suggested action.

Standalone callable. NOT wired into an Agent yet (VT-4 SDK skeleton is
Backlog). Importable as a function or class-method; tests mock the
Anthropic client.

VT-267 PR-B — prompt **v2.0** (Type-1 governance bump, logged in the decisions
ledger): added the ``first_data_step_onboarding`` intent (owner initiating the
first onboarding data step) + externalised the system prompt to
``prompts/classify_owner_message_v2.md`` (versioned via its metadata header, same
posture as self_evaluate + VT-63).

VT-595 — prompt **v4.0**: added the ``business_analysis`` intent (owner asking
WHICH/WHO customers or WHY, e.g. "which of my customers have stopped buying?" —
an analysis question, not a fact lookup) and tightened ``status_query`` to pure
count/fact lookups only. Fixes a defect where an analytical question keyed on
the word "customers" and short-circuited to a raw count via the
``edge_cases_router`` status_query fast-path instead of falling through to the
Team-Manager brain, which owns delegating analysis to the Sales-Recovery lane.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import UUID

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


Classification = Literal[
    "approval",
    "rejection",
    "question",
    "feedback",
    "first_data_step_onboarding",
    # VT-84 edge-case intents (routed by the stage-2 router to fast-path handlers).
    "exclusion_request",
    "adhoc_campaign_request",
    "status_query",
    "template_error_followup",
    # VT-595: an owner question that requires ANALYSIS over their data (WHICH/WHO/WHY),
    # not a pure count/fact lookup. Deliberately NOT in edge_cases_router's fast-path
    # branches and NOT in dispatch._ROUTINE_INTENTS — it falls through to the Opus-tier
    # brain, which owns delegating to the analysis lane (e.g. spawn_sales_recovery).
    "business_analysis",
    "other",
]


class ClassifyOwnerMessageInput(BaseModel):
    """Free-text owner message + locale hint.

    VT-270: ``tenant_id`` is REQUIRED — classification transmits the raw body to Anthropic (a
    sub-processor), so it sits behind the owner_inputs consent basis (CL-425), gated fail-closed.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(..., min_length=1, max_length=4000)
    tenant_id: str = Field(..., min_length=1)
    locale: str = "en-IN"


class ClassifyOwnerMessageOutput(BaseModel):
    """Typed envelope per VT-49 brief contract."""

    model_config = ConfigDict(frozen=True)

    classification: Classification
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_action: str
    # VT-270: set when classification was SKIPPED without transmit (e.g. no owner_inputs consent).
    skipped_reason: str | None = None


# VT-267 PR-B: system prompt externalised + versioned (v2.0), bumped to v3.0 (VT-84,
# first_data_step_onboarding) then v4.0 (VT-595, business_analysis label + status_query
# tightened to pure count/fact lookups). The version string lives in the file's
# metadata header (Type-1 governance change).
_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "classify_owner_message_v4.md"
)
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_MODEL = "claude-haiku-4-5-20251001"

# Markdown code-fence stripper. Haiku-4.5 intermittently wraps the JSON
# envelope in a ```json … ``` fence even though the prompt asks for bare
# JSON; an un-stripped fence makes json.loads raise, which (via
# resolve_decision_from_reply, a direct call in runner.py with no
# try/except) crashes the DBOS approval-resume step and strands the run
# 'running' forever. Mirrors the narrow fence-strip in sales_recovery.py
# (_CODE_FENCE_RE) but also tolerates the inline single-line form
# (```json{...}``` with no surrounding newlines) Haiku sometimes emits.
# NARROW by design: it only unwraps a recognised outer fence — it does NOT
# extract a JSON object from arbitrary surrounding prose (that would mask
# genuinely malformed output) and it never touches field VALUES (P8).
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n?(?P<body>.*?)\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_code_fence(raw: str) -> str:
    """Unwrap an optional outer ```json … ``` / ``` … ``` markdown fence.

    Returns the inner content (stripped) if ``raw`` is fully wrapped in a
    recognised fence; otherwise returns ``raw`` unchanged. Bare JSON passes
    through untouched.
    """
    match = _CODE_FENCE_RE.match(raw)
    if match is not None:
        return match.group("body").strip()
    return raw


def _skipped_envelope(reason: str) -> ClassifyOwnerMessageOutput:
    """VT-270: a no-transmit result — the body was NEVER sent to Anthropic. classification='other'
    maps to None ('leave paused') in resolve_decision_from_reply, so it's the conservative default."""
    return ClassifyOwnerMessageOutput(
        classification="other",
        confidence=0.0,
        suggested_action="classification skipped (no transmit)",
        skipped_reason=reason,
    )


def classify_owner_message(
    input: ClassifyOwnerMessageInput,
    *,
    client: Anthropic | None = None,
    consent_check: "Callable[[UUID], bool] | None" = None,
) -> ClassifyOwnerMessageOutput:
    """Classify an owner message.

    Args:
      input: typed message + tenant_id + locale
      client: optional Anthropic client (mockable for tests)
      consent_check: tenant owner_inputs gate (default ``_owner_inputs_enabled``); injectable for tests

    Returns:
      ClassifyOwnerMessageOutput envelope (Pydantic-validated). A SKIPPED envelope (no transmit)
      when owner_inputs consent is off / unverifiable.

    Raises:
      ValueError if the model returns invalid JSON or a non-conforming envelope

    VT-270 (CL-390/CL-425): classification transmits the RAW body to Anthropic (a sub-processor),
    so it is gated on the same owner_inputs basis as the L0 writer / vision pipeline — fail-CLOSED.
    No consent (or any consent-check error / bad tenant_id) → skip the transmit + return the skipped
    envelope; the body is never sent.
    """
    if consent_check is None:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        consent_check = _owner_inputs_enabled

    # VT-270 consent gate — BEFORE any transmit. Fail-closed on bad tenant_id / check error.
    try:
        allowed = consent_check(UUID(input.tenant_id))
    except Exception:  # noqa: BLE001 — any failure resolving/checking → fail-closed skip
        logger.info("classify_owner_message: consent check failed; skipping transmit (fail-closed)")
        return _skipped_envelope("consent_check_error")
    if not allowed:
        logger.info("classify_owner_message: owner_inputs disabled; skipping transmit")
        return _skipped_envelope("no_owner_inputs_consent")

    if client is None:
        client = Anthropic()

    resp = client.messages.create(
        model=_MODEL,
        max_tokens=200,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": input.text}],
    )
    text_blocks = [
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ]
    raw = "".join(text_blocks).strip()
    if not raw:
        raise ValueError("classify_owner_message: model returned empty content")

    # Haiku-4.5 may wrap the JSON envelope in a markdown code fence; unwrap it
    # before json.loads so a fenced response doesn't crash the resume path.
    raw = _strip_code_fence(raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"classify_owner_message: model returned non-JSON: {raw[:200]!r}"
        ) from exc

    try:
        return ClassifyOwnerMessageOutput(**parsed)
    except Exception as exc:
        raise ValueError(
            f"classify_owner_message: envelope validation failed: {parsed}"
        ) from exc


__all__ = [
    "Classification",
    "ClassifyOwnerMessageInput",
    "ClassifyOwnerMessageOutput",
    "classify_owner_message",
]
