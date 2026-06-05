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
"""

from __future__ import annotations

import json
import logging
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


# VT-267 PR-B: system prompt externalised + versioned (v2.0). The version string
# lives in the file's metadata header (Type-1 governance change).
_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "classify_owner_message_v3.md"
)
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_MODEL = "claude-haiku-4-5-20251001"


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
