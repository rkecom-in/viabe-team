"""VT-49 — classify_owner_message standalone tool.

Haiku-backed classifier. Takes a free-text owner message; emits a
typed envelope with classification + confidence + suggested action.

Standalone callable. NOT wired into an Agent yet (VT-4 SDK skeleton is
Backlog). Importable as a function or class-method; tests mock the
Anthropic client.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


Classification = Literal["approval", "rejection", "question", "feedback", "other"]


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


_SYSTEM_PROMPT = """\
You are a classifier for owner messages in the Viabe Team multi-agent system.

Your only job: read the owner's incoming message + return a JSON envelope
classifying their intent. The envelope MUST be a single JSON object with these
three fields and NOTHING else:

  classification: one of "approval" | "rejection" | "question" | "feedback" | "other"
  confidence: a float in [0.0, 1.0] reflecting your certainty
  suggested_action: a short (<= 80 chars) phrase describing what the orchestrator should do next

Definitions:
- approval: owner is saying yes to a pending campaign / plan / proposal
  ("yes go ahead", "looks good run it", "approved", "send it")
- rejection: owner is saying no
  ("no don't do that", "cancel it", "stop this", "scrap")
- question: owner is asking for information or clarification
  ("how does this work?", "what would it cost?", "what segment?")
- feedback: owner is commenting on a past run's outcome
  ("the timing was off", "wrong customers got targeted", "the message was confusing")
- other: greeting, off-topic, emoji-only, anything that doesn't fit the above

Output JSON only. No markdown fences. No prose preamble.

Examples:
Input: "yes go ahead with that"
Output: {"classification": "approval", "confidence": 0.95, "suggested_action": "execute the pending plan"}

Input: "no cancel that"
Output: {"classification": "rejection", "confidence": 0.95, "suggested_action": "abort the pending plan"}

Input: "what is this going to cost me?"
Output: {"classification": "question", "confidence": 0.9, "suggested_action": "answer cost question"}

Input: "the timing was wrong on yesterday's campaign"
Output: {"classification": "feedback", "confidence": 0.9, "suggested_action": "record feedback for next run"}

Input: "good morning"
Output: {"classification": "other", "confidence": 0.85, "suggested_action": "acknowledge greeting"}
"""

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
