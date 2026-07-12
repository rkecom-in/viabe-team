"""VT-628 — shared LLM sampling config.

Goal: pin ``temperature=0`` for deterministic manager behaviour where the model
allows it — run-to-run gate variance was traced partly to sampling at the API
default temperature on every call.

HARD API CONSTRAINT (verified constraint table — Anthropic live 2026-07-08; OpenAI docs 2026-07-13):
    claude-haiku-4-5     -> temperature ACCEPTED
    claude-sonnet-5      -> temperature DEPRECATED (400 "temperature is deprecated for this model")
    claude-opus-4-7/4-8  -> temperature DEPRECATED (400)
    gpt-5.6-*            -> temperature REJECTED (a reasoning model, like sonnet/opus)
So on the CURRENT multi-provider lineup ONLY haiku accepts the param. Determinism-via-temperature
is therefore available ONLY on haiku turns (the routine-brain + the intent
classifier). sonnet/opus/gpt-5.6 turns (brain-complex, triage, review, the specialist
lanes, the judge) CANNOT be pinned this way — their run-to-run variance is
irreducible via temperature and would need a different lever (N-sample, reasoning
effort, or a model that still honours the param).

Route EVERY Anthropic/ChatAnthropic AND OpenAI/ChatOpenAI call through this helper (via the
orchestrator.llm.provider seam) so a future model swap can never re-introduce a 400/400-class
temperature error (the bug this function exists to prevent).
"""

from __future__ import annotations

from typing import Any


def sampling_kwargs(model_id: str) -> dict[str, Any]:
    """Return sampling kwargs for an Anthropic / ChatAnthropic call.

    ``{"temperature": 0.0}`` for haiku (the only current family that accepts the
    param); ``{}`` for everything else (sonnet/opus 400 on it). Spread into the
    call/ctor: ``client.messages.create(model=m, **sampling_kwargs(m), ...)``.
    """
    if "haiku" in model_id.lower():
        return {"temperature": 0.0}
    return {}
