"""Migration-173 langchain usage callback → the per-call cost ledger.

``LlmUsageCallback`` is the seam the provider layer attaches to a langchain chat
model (ChatAnthropic / the GPT-5.6 provider). On ``on_llm_end`` it pulls this call's
usage — input/output tokens, model, provider request id — from the ``LLMResult`` and
calls ``record_llm_call`` so the call lands in ``llm_call_events`` (+ the VT-619
rollup). It is a pure observer: best-effort, never raises into the model call.

Constructor is ``(tenant_id, agent, call_site, service_tier='standard')`` — fixed for the provider
seam; ``service_tier`` is the call's BILLING tier so the ledger applies the flex discount.
Provider is inferred from the model id (``gpt*`` → openai, ``gemini*`` → google, ``glm*`` → zai,
``grok*`` → xai, else anthropic) since the constructor carries no provider arg; the ledger records
it on the event.

Migration-176: the callback ALSO pulls the server-side web/X-search invocation count from the
provider's usage surface (guarded, default 0), costs it via ``pricing.compute_search_cost``, and
threads ``search_count`` + ``search_cost_usd`` into ``record_llm_call`` (the new ledger columns).
Fully fail-soft — search metering never breaks a turn.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


class LlmUsageCallback(BaseCallbackHandler):
    """langchain callback that records each LLM call's usage to the cost ledger."""

    def __init__(
        self, tenant_id: Any, agent: str, call_site: str, service_tier: str = "standard"
    ) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.agent = agent
        self.call_site = call_site
        # The BILLING tier of the call (standard | flex). Must reach the ledger so cost_usd applies
        # the flex discount_multiplier — otherwise a flex call is costed at full rate (2x overstated).
        self.service_tier = service_tier

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            tokens_in, tokens_out, model, request_id, cached_in = _extract_usage(response)
            provider = _provider_for_model(model)
            search_count, search_cost = _extract_search_cost(response, provider)
            from orchestrator.llm.ledger import record_llm_call

            record_llm_call(
                tenant_id=self.tenant_id,
                agent=self.agent,
                call_site=self.call_site,
                provider=provider,
                model=model,
                service_tier=self.service_tier,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens_in=cached_in,
                request_id=request_id,
                search_count=search_count,
                search_cost_usd=search_cost,
            )
        except Exception:  # noqa: BLE001 — CL-122: metering never breaks a turn
            logger.warning("173 LlmUsageCallback.on_llm_end swallowed (best-effort)", exc_info=True)


def _provider_for_model(model: str | None) -> str:
    """Infer the provider from the model id (gpt* -> openai, gemini* -> google, glm* -> zai,
    grok* -> xai, else anthropic). Provider is NOT NULL on the ledger. The model id here is the
    response's ``model_name`` — ChatGoogleGenerativeAI reports the bare id (e.g. ``gemini-3.5-flash``);
    GLM's OpenAI-compatible response reports ``glm-5.2``; Grok's reports ``grok-4.5``."""
    lc = (model or "").lower()
    if lc.startswith("gpt"):
        return "openai"
    if lc.startswith("gemini"):
        return "google"
    if lc.startswith("glm"):
        return "zai"
    if lc.startswith("grok"):
        return "xai"
    return "anthropic"


def _extract_search_cost(response: Any, provider: str) -> tuple[int, Any]:
    """Return (total_search_count, total_search_cost_usd) for this call's server-side web/X searches.

    Pulls the server-side search-invocation count per provider from the langchain result surface
    (Migration-176), then costs each (tool, count) via ``pricing.compute_search_cost`` and sums. FULLY
    GUARDED — any missing surface / error yields (0, 0), never a raise (search metering must never
    break a turn). Most calls have no search → (0, 0), matching the ledger column defaults.

    Per-provider surface (SDK-verified 2026-07-13):
      * anthropic → ``llm_output['usage']['server_tool_use']['web_search_requests']``.
      * openai/xai (Responses) → count message content blocks whose type ends in ``_search_call``
        (``web_search_call`` verified; ``x_search_call`` mapped to x_search for xai).
      * google → 1 grounded request when ``response_metadata['grounding_metadata']`` carries any
        ``web_search_queries`` (Google grounding bills per REQUEST, not per query).
    """
    from decimal import Decimal

    try:
        counts = _search_invocation_counts(response, provider)
    except Exception:  # noqa: BLE001 — search extraction must never break metering
        logger.warning("176 search-count extraction swallowed (best-effort)", exc_info=True)
        return 0, 0
    if not counts:
        return 0, 0
    from orchestrator.llm.pricing import compute_search_cost

    total_count = 0
    total_cost = Decimal("0")
    for tool, n in counts.items():
        total_count += int(n or 0)
        total_cost += compute_search_cost(provider, tool, int(n or 0))
    return total_count, total_cost


def _first_message(response: Any) -> Any:
    """The first generation's chat message, or None. Guarded (used by the search extractors)."""
    gens = getattr(response, "generations", None)
    if gens and gens[0]:
        return getattr(gens[0][0], "message", None)
    return None


def _search_invocation_counts(response: Any, provider: str) -> dict[str, int]:
    """Map ``tool -> invocation count`` for this call's server-side searches. ``{}`` when none.
    Guarded per-provider; see ``_extract_search_cost`` for the surface each provider uses."""
    if provider == "anthropic":
        llm_output = getattr(response, "llm_output", None)
        usage = llm_output.get("usage") if isinstance(llm_output, dict) else None
        stu = usage.get("server_tool_use") if isinstance(usage, dict) else None
        n = int((stu or {}).get("web_search_requests") or 0) if isinstance(stu, dict) else 0
        return {"web_search": n} if n else {}

    if provider in ("openai", "xai"):
        msg = _first_message(response)
        content = getattr(msg, "content", None)
        counts: dict[str, int] = {}
        if isinstance(content, list):
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else None
                if isinstance(btype, str) and btype.endswith("_search_call"):
                    tool = "x_search" if btype.startswith("x_search") else "web_search"
                    counts[tool] = counts.get(tool, 0) + 1
        return counts

    if provider == "google":
        msg = _first_message(response)
        rmeta = getattr(msg, "response_metadata", None)
        gmeta = rmeta.get("grounding_metadata") if isinstance(rmeta, dict) else None
        queries = gmeta.get("web_search_queries") if isinstance(gmeta, dict) else None
        # Google grounding is billed per grounded REQUEST, not per query → count 1 when grounded.
        return {"web_search": 1} if queries else {}

    return {}


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
