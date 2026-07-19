"""VT-619b — the multi-provider LLM seam.

Every model construction in the orchestrator routes through ``provider.resolve_chat_model``
(langchain ChatAnthropic / ChatOpenAI) or ``provider.resolve_model_id`` (the raw Anthropic
Messages SDK sites). The provider (anthropic | openai) is inferred from the model-id prefix and
the concrete id is selected per-call from the ``TEAM_MODEL_*`` env tier mapping — so a model swap
(claude-* ↔ gpt-5.6-*) is a Railway ENV change, never a code change.

Public API is re-exported here so callers import from ``orchestrator.llm``:

    from orchestrator.llm import resolve_chat_model, resolve_model_id, BudgetExceededError
"""

from __future__ import annotations

from orchestrator.llm.provider import (
    ANTHROPIC_MODELS,
    OPENAI_MODELS,
    SUPPORTED_MODELS,
    BudgetExceededError,
    UnknownModelError,
    enforce_budget,
    invoke_with_flex_fallback,
    provider_for,
    require_anthropic_model,
    resolve_chat_model,
    resolve_model_id,
)

__all__ = [
    "ANTHROPIC_MODELS",
    "OPENAI_MODELS",
    "SUPPORTED_MODELS",
    "BudgetExceededError",
    "UnknownModelError",
    "enforce_budget",
    "invoke_with_flex_fallback",
    "provider_for",
    "require_anthropic_model",
    "resolve_chat_model",
    "resolve_model_id",
]
