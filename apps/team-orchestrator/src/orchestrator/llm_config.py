"""VT-628 — shared LLM sampling config.

Goal: pin ``temperature=0`` for deterministic manager behaviour where the model
allows it — run-to-run gate variance was traced partly to sampling at the API
default temperature on every call.

HARD API CONSTRAINT (verified constraint table — Anthropic live 2026-07-08; OpenAI docs 2026-07-13;
Google SDK-verified 2026-07-13, langchain-google-genai==4.2.5 — NOT live-verified):
    claude-haiku-4-5     -> temperature ACCEPTED
    claude-sonnet-5      -> temperature DEPRECATED (400 "temperature is deprecated for this model")
    claude-opus-4-7/4-8  -> temperature DEPRECATED (400)
    gpt-5.6-*            -> temperature REJECTED (a reasoning model, like sonnet/opus)
    gemini-3.5/3.1-*     -> temperature ACCEPTED (SDK-verified; ctor default 0.7, we pin 0.0)
    glm-5.2              -> temperature ACCEPTED (SDK-verified; we pin 0.0)
    grok-4.5/4.3         -> temperature ACCEPTED (docs.x.ai-verified 2026-07-13; we pin 0.0)
So on the CURRENT multi-provider lineup haiku AND the gemini-* / glm-* / grok-* families accept the
param. Determinism-via-temperature is therefore available on haiku turns (the routine-brain +
the intent classifier) and on any gemini-* / glm-* / grok-* turn. sonnet/opus/gpt-5.6 turns (brain-complex,
triage, review, the specialist lanes, the judge) CANNOT be pinned this way — their run-to-run
variance is irreducible via temperature and would need a different lever (N-sample, reasoning
effort, or a model that still honours the param).

Route EVERY Anthropic/ChatAnthropic AND OpenAI/ChatOpenAI call through this helper (via the
orchestrator.llm.provider seam) so a future model swap can never re-introduce a 400/400-class
temperature error (the bug this function exists to prevent).
"""

from __future__ import annotations

from typing import Any


def sampling_kwargs(model_id: str) -> dict[str, Any]:
    """Return sampling kwargs for an Anthropic / OpenAI / Google / Z.ai / xAI chat call.

    ``{"temperature": 0.0}`` for the families that ACCEPT the param — haiku (Anthropic),
    gemini-* (Google), glm-* (Z.ai), and grok-* (xAI) — for determinism; ``{}`` for everything else
    (sonnet/opus/gpt-5.6 400/reject on it). Spread into the call/ctor:
    ``client.messages.create(model=m, **sampling_kwargs(m), ...)``.
    """
    lc = model_id.lower()
    if "haiku" in lc or "gemini" in lc or "glm" in lc or "grok" in lc:
        return {"temperature": 0.0}
    return {}
