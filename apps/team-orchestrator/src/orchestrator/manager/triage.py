"""VT-606 (Loop Package 3) — the manager turn-triage classifier.

"Manager turn handling" steps 1-4 (execution-plan §3): before anything else, opt-out/DSR/delivery/
approval handlers already ran (runner.py's deterministic gates, upstream of this — this module is
NEVER reached for those; it triages only what already fell through to the brain path). Given the
inbound conversation turn + two deterministic priors, classify into ONE of FIVE outcomes.

Fail-soft (binding, execution-plan §3 step 4 + the VT-606 dispatch): ANY classify failure —
malformed JSON, a validation error, a raised exception from the Anthropic call — returns ``None``.
The caller's contract is: ``None`` means "fall back to the CURRENT dispatch behavior" (the legacy
graph.invoke() path runs exactly as it does today) — NEVER a new silent path, NEVER a guessed
classification. Mirrors ``agent.tools.classify_owner_message``'s house pattern (raw
``Anthropic().messages.create`` + JSON parse + pydantic validation), not ``with_structured_output``
(unused anywhere in this codebase).

Consent: this module transmits the owner's inbound TEXT to Anthropic — the SAME transmit class
``classify_owner_message`` gates on ``owner_inputs`` consent (VT-270/CL-425). It does NOT
re-implement that gate: by the time triage would run (post ``runner._brain_owner_inputs_ok``), the
SAME turn has already passed it upstream — this module is reached only from the brain path that
gate already admitted.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger("orchestrator.manager.triage")

_TRIAGE_MODEL = "claude-sonnet-5"  # A5 — triage stays on the Sonnet-5 tier.
_MAX_TOKENS = 150

_PROMPT_PATH = Path(__file__).parent / "prompts" / "manager_triage.md"
_TRIAGE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n?(?P<body>.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE
)


def _strip_code_fence(raw: str) -> str:
    match = _CODE_FENCE_RE.match(raw)
    return match.group("body").strip() if match is not None else raw


TriageOutcome = Literal["direct_reply", "answer_pending", "new_task", "task_status", "cancel_task"]


class TriageResult(BaseModel):
    """The structured triage envelope (execution-plan §3 step 4)."""

    model_config = ConfigDict(extra="forbid")

    outcome: TriageOutcome
    reasoning: str = ""


def triage_turn(
    *,
    message_text: str,
    has_open_question: bool,
    has_active_task: bool,
    client: Anthropic | None = None,
) -> TriageResult | None:
    """Classify one inbound owner turn. Returns ``None`` on ANY failure (fail-soft — see module
    docstring); the caller must treat that as "run the legacy path, do nothing new," never as a
    default classification value."""
    if client is None:
        client = Anthropic()

    user_content = (
        f"has_open_question: {has_open_question}\n"
        f"has_active_task: {has_active_task}\n\n"
        f"Owner message:\n{message_text}"
    )
    try:
        resp = client.messages.create(
            model=_TRIAGE_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,  # VT-628 — deterministic (sonnet accepts temperature; opus would 400)
            system=_TRIAGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception:  # noqa: BLE001 — fail-soft: any transport/API error -> None, never a raise
        logger.warning("triage_turn: Anthropic call failed (fail-soft -> None)", exc_info=True)
        return None

    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "".join(text_blocks).strip()
    if not raw:
        logger.warning("triage_turn: empty model output (fail-soft -> None)")
        return None
    raw = _strip_code_fence(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("triage_turn: non-JSON model output (fail-soft -> None): %r", raw[:200])
        return None
    try:
        result = TriageResult(**parsed)
    except ValidationError:
        logger.warning("triage_turn: envelope validation failed (fail-soft -> None): %r", parsed)
        return None

    # A deterministic sanity backstop over the LLM's own judgment: 'answer_pending' is meaningless
    # without an actually-open question — never let the classifier invent one. Fail-soft to None
    # (not a silent re-label) so the caller's "no new path" contract holds.
    if result.outcome == "answer_pending" and not has_open_question:
        logger.warning(
            "triage_turn: classified answer_pending with has_open_question=False "
            "(fail-soft -> None, treating as a classify miss)"
        )
        return None

    return result


__all__ = ["TriageOutcome", "TriageResult", "triage_turn"]
