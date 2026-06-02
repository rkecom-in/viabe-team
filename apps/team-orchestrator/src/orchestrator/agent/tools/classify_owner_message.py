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
from pathlib import Path
from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


Classification = Literal[
    "approval",
    "rejection",
    "question",
    "feedback",
    "first_data_step_onboarding",
    "other",
]


class ClassifyOwnerMessageInput(BaseModel):
    """Free-text owner message + locale hint."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(..., min_length=1, max_length=4000)
    locale: str = "en-IN"


class ClassifyOwnerMessageOutput(BaseModel):
    """Typed envelope per VT-49 brief contract."""

    model_config = ConfigDict(frozen=True)

    classification: Classification
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_action: str


# VT-267 PR-B: system prompt externalised + versioned (v2.0). The version string
# lives in the file's metadata header (Type-1 governance change).
_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "classify_owner_message_v2.md"
)
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_MODEL = "claude-haiku-4-5-20251001"


def classify_owner_message(
    input: ClassifyOwnerMessageInput,
    *,
    client: Anthropic | None = None,
) -> ClassifyOwnerMessageOutput:
    """Classify an owner message.

    Args:
      input: typed message + locale
      client: optional Anthropic client (mockable for tests)

    Returns:
      ClassifyOwnerMessageOutput envelope (Pydantic-validated)

    Raises:
      ValueError if the model returns invalid JSON or a non-conforming envelope
    """
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
