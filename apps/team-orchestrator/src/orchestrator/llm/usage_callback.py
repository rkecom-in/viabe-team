"""Migration-173 langchain usage callback → the per-call cost ledger.

``LlmUsageCallback`` is the seam the provider layer attaches to a langchain chat
model (ChatAnthropic / the GPT-5.6 provider). On ``on_llm_end`` it pulls this call's
usage — input/output tokens, model, provider request id — from the ``LLMResult`` and
calls ``record_llm_call`` so the call lands in ``llm_call_events`` (+ the VT-619
rollup). It is a pure observer: best-effort, never raises into the model call.

Constructor is ``(tenant_id, agent, call_site)`` — fixed for the provider seam.
Provider is inferred from the model id (``gpt*`` → openai, else anthropic) since the
constructor carries no provider arg; the ledger records it on the event.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


class LlmUsageCallback(BaseCallbackHandler):
    """langchain callback that records each LLM call's usage to the cost ledger."""

    def __init__(self, tenant_id: Any, agent: str, call_site: str) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.agent = agent
        self.call_site = call_site

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            tokens_in, tokens_out, model, request_id, cached_in = _extract_usage(response)
            from orchestrator.llm.ledger import record_llm_call

            record_llm_call(
                tenant_id=self.tenant_id,
                agent=self.agent,
                call_site=self.call_site,
                provider=_provider_for_model(model),
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens_in=cached_in,
                request_id=request_id,
            )
        except Exception:  # noqa: BLE001 — CL-122: metering never breaks a turn
            logger.warning("173 LlmUsageCallback.on_llm_end swallowed (best-effort)", exc_info=True)


def _provider_for_model(model: str | None) -> str:
    """Infer the provider from the model id. Provider is NOT NULL on the ledger."""
    return "openai" if (model or "").lower().startswith("gpt") else "anthropic"


def _extract_usage(response: Any) -> tuple[int, int, str, str | None, int]:
    """Pull (tokens_in, tokens_out, model, request_id, cached_tokens_in) from a
    langchain LLMResult.

    Reads the chat message's ``usage_metadata`` (input_tokens/output_tokens +
    ``input_token_details.cache_read``) + ``response_metadata`` (model_name/model +
    id) first — the surface ChatAnthropic and the OpenAI chat wrapper populate —
    then falls back to ``llm_output`` (token_usage/usage) for providers/versions that
    land usage there. Every access is guarded; a missing surface yields zeros /
    ``"unknown"`` model, never a raise.

    langchain's normalized ``input_tokens`` is the TOTAL prompt count INCLUDING
    cache reads, with ``input_token_details.cache_read`` the cached subset. We return
    ``tokens_in`` = full-price (total − cache_read) and ``cached_tokens_in`` =
    cache_read, so the ledger prices the cached portion at ~0.1× (both providers).
    The ``llm_output`` fallback does not attempt a cache split (cached=0, full price)
    — conservative, avoids provider-specific double-counting.
    """
    total_in = tokens_out = cached_in = 0
    model: str | None = None
    request_id: str | None = None

    gens = getattr(response, "generations", None)
    msg = None
    if gens and gens[0]:
        msg = getattr(gens[0][0], "message", None)
    if msg is not None:
        um = getattr(msg, "usage_metadata", None)
        if isinstance(um, dict):
            total_in = int(um.get("input_tokens") or 0)
            tokens_out = int(um.get("output_tokens") or 0)
            details = um.get("input_token_details")
            if isinstance(details, dict):
                cached_in = int(details.get("cache_read") or 0)
        rmeta = getattr(msg, "response_metadata", None)
        if isinstance(rmeta, dict):
            model = rmeta.get("model_name") or rmeta.get("model") or model
            request_id = rmeta.get("id") or request_id
        if request_id is None:
            request_id = getattr(msg, "id", None)

    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        model = model or llm_output.get("model_name") or llm_output.get("model")
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if isinstance(usage, dict):
            if not total_in:
                total_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            if not tokens_out:
                tokens_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)

    # Split total input into full-price vs cache-read (never negative).
    cached_in = min(cached_in, total_in)
    tokens_in = total_in - cached_in
    return tokens_in, tokens_out, model or "unknown", request_id, cached_in


__all__ = ["LlmUsageCallback"]
