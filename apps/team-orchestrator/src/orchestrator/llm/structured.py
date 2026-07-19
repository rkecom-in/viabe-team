"""Provider-aware single-shot text completion for the JSON-in-prose gate sites.

triage / classify_owner_message / plan_validation each build a system+user prompt, call the LLM
once, and ``json.loads`` the raw text (they do NOT use tools / with_structured_output). Historically
they called ``anthropic.Anthropic().messages.create`` directly, which (a) locked those tiers to
Anthropic and (b) bypassed the migration-173 cost ledger.

``structured_text_call`` routes the call through the multi-provider seam
(``resolve_chat_model``) so any of the 5 providers works (gpt-5.6 / gemini / glm / grok / claude,
env-selected per tier) AND the call is metered — the seam attaches the ledger callback. Callers keep
their own prompt, fence-strip, ``json.loads`` and pydantic validation unchanged; only the transport
moved here.
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


def structured_text_call(
    tier: str,
    *,
    system: str,
    user: str,
    max_tokens: int,
    agent: str,
    call_site: str,
    tenant_id: UUID | str | None = None,
) -> str:
    """Resolve the tier's model via the multi-provider seam, invoke it once with ``system`` + ``user``
    and return the raw response text. Raises ``ValueError`` on an empty response (callers fail-soft).
    The call is cost-metered through the seam's ledger callback (``call_site`` labels the ledger row).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from orchestrator.llm.provider import resolve_chat_model

    model = resolve_chat_model(
        tier, agent=agent, tenant_id=tenant_id, max_tokens=max_tokens, call_site=call_site
    )
    resp = model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    text = _content_to_text(getattr(resp, "content", ""))
    if not text.strip():
        raise ValueError(f"empty response from {call_site} ({tier}) call")
    return text


__all__ = ["structured_text_call"]
