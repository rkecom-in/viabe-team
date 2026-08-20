"""Provider-aware text completion for the direct-SDK call sites.

triage / classify_owner_message / plan_validation each build a system+user prompt, call the LLM
once, and ``json.loads`` the raw text (they do NOT use tools / with_structured_output). Historically
they called ``anthropic.Anthropic().messages.create`` directly, which (a) locked those tiers to
Anthropic and (b) bypassed the migration-173 cost ledger.

``structured_text_call`` routes the call through the multi-provider seam
(``resolve_chat_model``) so any of the 5 providers works (gpt-5.6 / gemini / glm / grok / claude,
env-selected per tier) AND the call is metered — the seam attaches the ledger callback. Callers keep
their own prompt, fence-strip, ``json.loads`` and pydantic validation unchanged; only the transport
moved here.

VT-732 — the seam grew the four things the bypassing sites actually needed, which is WHY they had
stayed on the raw SDK (a port that dropped any of them would be a downgrade dressed as governance):

  * ``timeout_s`` — every hot-path classifier passed ``timeout=``; a hang there is a stalled owner
    turn, not a slow one.
  * ``cache_system`` — the onboarding brain rides its ~6KB system prompt as ONE ``cache_control``
    block (cache batch 2026-07-18). Applied only on anthropic (the other providers cache
    automatically / have no such block), so the cost win survives the port where it exists.
  * ``text_mode="last"`` — the server-side-search sites parse the LAST text block (earlier blocks are
    "I'll search…" preamble), not the concatenation.
  * ``messages_call`` — the multi-turn / tool-loop / vision sites, which need the raw response object
    (tool calls, stop reason) rather than text.

Message shapes stay LANGCHAIN-NATIVE here, so a tool loop written against this module runs on any
provider: tool definitions are converted per provider by langchain-core (an Anthropic-shaped
``input_schema`` dict converts cleanly to the OpenAI function shape — verified against the installed
pins), and tool calls come back on the standard ``AIMessage.tool_calls``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    """Normalize a langchain message ``.content`` to a plain string. ChatAnthropic may return a list
    of blocks (``{"type": "text", "text": ...}``); the OpenAI/Gemini/GLM/Grok wrappers return a
    string. Non-text blocks (reasoning, tool calls) are skipped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _last_text_block(content: Any) -> str:
    """The LAST non-empty text block of a response — the parse the server-side-search sites use.

    A web_search / web_fetch response interleaves the model's "I'll look that up…" preamble, the
    server tool blocks, and THEN the real answer; concatenating them feeds the preamble into a
    ``json.loads``. Anything that is already a plain string has exactly one block, so this is the
    same answer as ``_content_to_text`` for the non-search callers.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, str) and block.strip():
                return block
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                if text.strip():
                    return text
        return ""
    return str(content or "")


def _system_message(system: str | list[dict[str, Any]], *, cache: bool, provider: str) -> Any:
    """The system turn, as a ``cache_control`` block on anthropic when ``cache`` is asked for.

    Prompt caching is expressed as a content BLOCK in the Anthropic API; the other providers cache
    prefixes automatically and reject the block, so a cached system silently becomes a plain string
    there. That is a cost difference, never a behaviour difference — the text is identical.

    ``system`` may already BE a block list (the sales-recovery prompt is
    ``[static cache_control block, volatile dated block]`` so the daily render never busts the cached
    prefix). That list passes through untouched on anthropic and is flattened to its concatenated
    text everywhere else.
    """
    from langchain_core.messages import SystemMessage

    if isinstance(system, list):
        if provider == "anthropic":
            return SystemMessage(content=system)
        return SystemMessage(
            content="".join(str(b.get("text", "")) for b in system if isinstance(b, dict))
        )
    if cache and provider == "anthropic":
        return SystemMessage(
            content=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        )
    return SystemMessage(content=system)


def _bind_tools(model: Any, provider: str, tools: list[dict[str, Any]] | None,
                native_tools: dict[str, list[dict[str, Any]]] | None) -> Any:
    """Bind portable function ``tools`` (converted per provider by langchain-core) plus any
    ``native_tools`` entry matching the RESOLVED provider.

    ``native_tools`` is keyed by provider name (e.g. ``{"anthropic": [web_fetch_spec]}``) because a
    server-side builtin is provider-native by definition: binding Anthropic's ``web_fetch`` spec to
    an OpenAI model is a 400, and fabricating an equivalent is worse. A native tool whose provider is
    not the resolved one is logged + dropped — the call still runs, with one less capability, which is
    the honest degradation. Callers that need the capability itself must pin an Anthropic tier.
    """
    native: list[dict[str, Any]] = []
    for tool_provider, specs in (native_tools or {}).items():
        if tool_provider == provider:
            native.extend(specs)
        else:
            logger.info(
                "native tools for provider %r dropped — resolved provider is %r", tool_provider, provider
            )
    if not tools and not native:
        return model
    if provider in ("openai", "xai"):
        # ONE .bind — parity with provider._apply_search_tools, and because a second .bind(tools=…)
        # would REPLACE the first's tools rather than extend them. Function specs are converted here
        # (langchain-core raises on some builtin specs, so those bypass the converter untouched).
        from langchain_core.utils.function_calling import convert_to_openai_tool

        payload = [convert_to_openai_tool(t) for t in (tools or [])] + native
        return model.bind(tools=payload)
    return model.bind_tools(list(tools or []) + native)


def structured_text_call(
    tier: str,
    *,
    system: str | None = None,
    user: str,
    max_tokens: int,
    agent: str,
    call_site: str,
    tenant_id: UUID | str | None = None,
    timeout_s: float | None = None,
    cache_system: bool = False,
    enable_web_search: bool = False,
    text_mode: str = "join",
) -> str:
    """Resolve the tier's model via the multi-provider seam, invoke it once with ``system`` + ``user``
    and return the raw response text. Raises ``ValueError`` on an empty response (callers fail-soft).
    The call is cost-metered through the seam's ledger callback (``call_site`` labels the ledger row).

    ``system`` is optional (many ported classifiers are a single user prompt). ``timeout_s`` bounds the
    request. ``cache_system`` rides the system prompt as an anthropic ``cache_control`` block.
    ``enable_web_search`` asks for the provider's native server-side search (still subject to the
    ``TEAM_ENABLE_WEB_SEARCH`` master flag). ``text_mode="last"`` returns the last text block.
    """
    from langchain_core.messages import HumanMessage

    from orchestrator.llm.provider import provider_for, resolve_chat_model, resolve_model_id

    provider = provider_for(resolve_model_id(tier))
    model = resolve_chat_model(
        tier,
        agent=agent,
        tenant_id=tenant_id,
        max_tokens=max_tokens,
        call_site=call_site,
        timeout_s=timeout_s,
        enable_web_search=enable_web_search,
    )
    messages: list[Any] = []
    if system:
        messages.append(_system_message(system, cache=cache_system, provider=provider))
    messages.append(HumanMessage(content=user))
    resp = model.invoke(messages)
    content = getattr(resp, "content", "")
    text = _last_text_block(content) if text_mode == "last" else _content_to_text(content)
    if not text.strip():
        # Name the MODEL and the stop reason. "empty response from triage (complex)" cost real time
        # on dev because it did not say WHICH model answered or WHY it stopped — the answer was
        # "gpt-5.6-luna, incomplete: the max_output_tokens cap was spent on reasoning".
        meta = getattr(resp, "response_metadata", None) or {}
        stop = meta.get("stop_reason") or meta.get("finish_reason") or "unknown"
        raise ValueError(
            f"empty response from {call_site} ({tier}) call: model={resolve_model_id(tier)!r} "
            f"stop_reason={stop!r} max_tokens={max_tokens}"
        )
    return text


def messages_call(
    tier: str,
    *,
    messages: list[Any],
    system: str | None = None,
    max_tokens: int,
    agent: str,
    call_site: str,
    tenant_id: UUID | str | None = None,
    timeout_s: float | None = None,
    cache_system: bool = False,
    tools: list[dict[str, Any]] | None = None,
    native_tools: dict[str, list[dict[str, Any]]] | None = None,
    betas: list[str] | None = None,
    enable_web_search: bool = False,
) -> Any:
    """The multi-turn / tool-loop / vision seam: invoke ``tier``'s model on a langchain message list
    and return the RAW response (an ``AIMessage``), so the caller can read ``.tool_calls``,
    ``.content`` blocks and ``response_metadata`` itself.

    ``messages`` are langchain message objects (``HumanMessage`` / ``AIMessage`` / ``ToolMessage``);
    an image turn is an ordinary ``HumanMessage`` whose content carries a standard image block, which
    each provider adapter translates. ``system`` is prepended (optionally cached). ``tools`` are
    portable function specs; ``native_tools`` / ``betas`` are the provider-native extras (see
    ``_bind_tools`` and ``resolve_chat_model``).
    """
    from orchestrator.llm.provider import provider_for, resolve_chat_model, resolve_model_id

    provider = provider_for(resolve_model_id(tier))
    model = resolve_chat_model(
        tier,
        agent=agent,
        tenant_id=tenant_id,
        max_tokens=max_tokens,
        call_site=call_site,
        timeout_s=timeout_s,
        betas=betas if provider == "anthropic" else None,
        enable_web_search=enable_web_search,
    )
    model = _bind_tools(model, provider, tools, native_tools)
    full: list[Any] = []
    if system:
        full.append(_system_message(system, cache=cache_system, provider=provider))
    full.extend(messages)
    return model.invoke(full)


def response_text(resp: Any, *, mode: str = "join") -> str:
    """Text of a ``messages_call`` response — ``join`` (all text blocks) or ``last`` (the final one)."""
    content = getattr(resp, "content", "")
    return _last_text_block(content) if mode == "last" else _content_to_text(content)


# ---------------------------------------------------------------------------
# SDK-shaped view of a seam response
# ---------------------------------------------------------------------------
# The agent loops built before the seam (sales_recovery, business_plan) do their accounting against
# the Anthropic response OBJECT: ``.content`` blocks, ``.stop_reason``, ``.usage.input_tokens``. Their
# per-turn budget enforcement, continuation handling and cost attribution all read those attributes,
# and rewriting that accounting is a much bigger change than swapping the transport — the kind that
# quietly moves a money-path behaviour while claiming to be a governance fix. This adapter is the
# narrow bridge: same fields, filled from langchain's provider-neutral equivalents.
_STOP_REASON_ALIASES: dict[str, str] = {
    # OpenAI/xAI/GLM finish_reason -> the Anthropic vocabulary these loops branch on.
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    # google
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
}


class _SdkTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _SdkToolUseBlock:
    type = "tool_use"

    def __init__(self, call: dict[str, Any]) -> None:
        self.id = str(call.get("id") or "")
        self.name = str(call.get("name") or "")
        self.input = call.get("args") or {}


class _SdkUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class SdkShapedResponse:
    """``.content`` / ``.stop_reason`` / ``.usage`` over a langchain response."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        text = _content_to_text(getattr(resp, "content", ""))
        blocks: list[Any] = [_SdkTextBlock(text)] if text else []
        blocks.extend(
            _SdkToolUseBlock(c) for c in (getattr(resp, "tool_calls", None) or []) if isinstance(c, dict)
        )
        self.content = blocks
        meta = getattr(resp, "response_metadata", None) or {}
        raw_stop = str(meta.get("stop_reason") or meta.get("finish_reason") or "") or None
        self.stop_reason = _STOP_REASON_ALIASES.get(raw_stop, raw_stop) if raw_stop else None
        usage = getattr(resp, "usage_metadata", None) or {}
        self.usage = _SdkUsage(
            int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
        )


def as_sdk_response(resp: Any) -> SdkShapedResponse:
    """Adapt a seam response to the Anthropic-SDK shape the pre-seam agent loops read."""
    return SdkShapedResponse(resp)


__all__ = [
    "SdkShapedResponse",
    "as_sdk_response",
    "messages_call",
    "response_text",
    "structured_text_call",
]
