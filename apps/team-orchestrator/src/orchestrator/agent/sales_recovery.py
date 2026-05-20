"""Sales Recovery specialist — Agent SDK skeleton (VT-32).

This module is the real specialist that the orchestrator's specialist
dispatch will eventually call (currently still routed through the stub —
see ``sales_recovery_stub.py``; switching dispatch call sites is a later
subtask).

Tier 2 plumbing only (CL-242)
-----------------------------
This module MUST NOT touch the database, send WhatsApp messages, or
mutate LangGraph state directly. It receives a typed context, runs an
agent loop on the Anthropic Messages API, and returns a typed
``AgentResult``. The orchestrator owns persistence + side effects.

VT-35 hook seams
----------------
The two well-named functions below are the seams VT-35's four hard-limit
enforcers attach to. Do NOT collapse them into a single opaque call:

  - ``_run_one_turn`` — the *per-turn boundary*. Each call is one
    Messages.create round-trip. The depth tracker and token meter
    instrument here.
  - ``_dispatch_tool`` — the *tool-dispatch seam*. Each call is one tool
    invocation (success OR failure). The tool counter instruments here.

The wall-clock timer attaches at ``run_sales_recovery_agent`` entry/exit
(it watches the whole run); the cancel coordinator orchestrates a clean
break across all four enforcers.

The placeholder prompt
----------------------
This PR ships with a placeholder system prompt that asks the model to
emit ``{"status": "placeholder"}`` and stop. The real prompt is a later
subtask. The placeholder text is intentionally short and free of
instruction-tuning: it is for plumbing validation, not behaviour
validation.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from anthropic import Anthropic

from orchestrator.agent.cost import compute_cost_paise
from orchestrator.agent.types import AgentResult

# Exactly the placeholder text required by the VT-32 brief. Do not edit
# without owning the brief — this prompt is a Type-3 commitment for the
# canary / plumbing-validation path.
_PLACEHOLDER_SYSTEM_PROMPT = (
    "You are a placeholder agent. Reply with the JSON "
    '{"status": "placeholder"}. Do nothing else.'
)

# Per-response output cap passed to ``messages.create``. Distinct from the
# run-level hard-limit ceiling below — ``max_tokens`` here is "max length
# of ONE response", which is what the Messages API expects (passing the
# 80K run-level budget here also trips the SDK's non-streaming 10-minute
# timeout guard). The placeholder canary response is ~10 tokens; 1024 is
# generous headroom. Real-prompt tuning lands with the real prompt.
_MAX_OUTPUT_TOKENS_PER_TURN = 1024

# Run-level hard-limit ceiling. VT-35's token meter enforces a CUMULATIVE
# 80K cap across every turn in one run. This constant lives here only as
# a documented reference for AgentResult semantics (CL-242); it is NOT
# wired into any SDK call. VT-35 will read this when wiring the token
# meter. Renaming this constant requires updating VT-35's enforcer.
_RUN_LEVEL_TOKEN_HARD_LIMIT = 80_000

# Extended-thinking budget. ``max_thinking_tokens`` in the brief maps to
# the Messages-API ``thinking.budget_tokens`` field. Conservative budget
# for the placeholder canary; the real prompt subtask will tune this.
_THINKING_BUDGET_TOKENS = 16_000

# Cap turns at a small number for placeholder runs — without real tools
# the model has nothing to chain, so one turn is enough. VT-35's depth
# limit (≤ 8) is the hard cap once real tools land; this is a safety
# rail for the empty-tool skeleton.
_MAX_TURNS_PLACEHOLDER = 4


_MODELS_YAML = (
    Path(__file__).resolve().parents[3] / "config" / "models.yaml"
)


@dataclass
class SalesRecoveryContext:
    """Placeholder context type for VT-32.

    The real ``SalesRecoveryContext`` bundle (full Context Composer
    output) is a later subtask. For VT-32 the agent is run with a
    placeholder prompt and no tools, so it does not consume context
    fields — but the function signature MUST be stable so dispatch
    callers can wire to it now and the bundle can fill in later.

    ``tenant_id`` and ``run_id`` are required (Pillar 3 — every run is
    tenant-scoped; the orchestrator never invokes a specialist without
    them); other fields land later.
    """

    tenant_id: str
    run_id: str


def _resolve_model(agent_name: str = "sales_recovery") -> str:
    """Return the model id for ``agent_name`` per ``VIABE_ENV``.

    ``VIABE_ENV in {'production'}`` → ``production`` slot; everything else
    (test/dev/canary or unset) → ``test`` slot. The unset default is
    test/Haiku — never silently fall through to Opus in a development
    environment.
    """
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config[agent_name][slot])


def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    tools: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call. VT-35 tool-counter seam.

    For VT-32 ``tools`` is always ``{}`` (no real tools yet). Calling
    this with an empty registry returns a structured ``tool_error``
    result so the agent loop can append it as a ``tool_result`` and
    finish cleanly — instead of raising and unwinding the loop.

    VT-35's tool counter wraps this function: every call increments the
    counter regardless of whether the dispatch succeeded.
    """
    if tool_name not in tools:
        return {
            "tool_name": tool_name,
            "is_error": True,
            "content": f"unknown tool: {tool_name}",
        }
    handler = tools[tool_name]
    try:
        return cast(dict[str, Any], handler(tool_input))
    except Exception as exc:  # noqa: BLE001 — surface as tool_error result
        return {"tool_name": tool_name, "is_error": True, "content": str(exc)}


def _run_one_turn(
    client: Anthropic,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
) -> Any:
    """One Messages.create round-trip. VT-35 per-turn / token-meter seam.

    Isolated so VT-35's enforcers can instrument exactly one turn at a
    time and so tests can mock at this boundary (zero real API calls in
    CI by patching this function).
    """
    # mypy: anthropic.Messages.create overloads are TypedDict-heavy
    # (MessageParam, ThinkingConfigEnabledParam) — typing the plain-dict
    # messages list to match would add noise without value for a Phase 1
    # placeholder loop. The shape is asserted at runtime by the SDK.
    return client.messages.create(  # type: ignore[call-overload]
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS_PER_TURN,
        thinking={"type": "enabled", "budget_tokens": _THINKING_BUDGET_TOKENS},
        system=system_prompt,
        messages=messages,
        tools=[],
    )


def _extract_text(content_blocks: list[Any]) -> str:
    """Concatenate every TextBlock's text from a response's content."""
    out: list[str] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _parse_placeholder_output(text: str) -> dict[str, Any] | None:
    """Best-effort parse of the placeholder JSON. Returns None on failure."""
    import json

    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def run_sales_recovery_agent(context: SalesRecoveryContext) -> AgentResult:
    """Run the sales_recovery specialist; return a structured ``AgentResult``.

    Hand-written agent loop on the Anthropic Messages API (CL-242).
    Tier-2 plumbing: no DB, no side effects — the orchestrator owns
    those. The orchestrator measures (VT-35 hard limits attach here),
    the agent does not see its own usage.

    For VT-32 the loop is intentionally minimal — placeholder prompt,
    empty tools — but it preserves the per-turn boundary
    (``_run_one_turn``) and tool-dispatch seam (``_dispatch_tool``) so
    VT-35 enforcers attach without re-architecture.
    """
    start = time.monotonic()
    client = Anthropic()
    model = _resolve_model("sales_recovery")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "begin"},
    ]
    tools: dict[str, Any] = {}  # VT-32: no real tools yet.
    raw_messages: list[dict[str, Any]] = list(messages)

    input_tokens_used = 0
    output_tokens_used = 0
    tool_calls_made = 0
    status: str = "completed"
    output: dict[str, Any] | None = None

    for _ in range(_MAX_TURNS_PLACEHOLDER):
        response = _run_one_turn(
            client,
            model=model,
            system_prompt=_PLACEHOLDER_SYSTEM_PROMPT,
            messages=messages,
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens_used += int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens_used += int(getattr(usage, "output_tokens", 0) or 0)

        content_blocks = list(getattr(response, "content", []) or [])
        raw_messages.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content_blocks]}
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []
            for block in content_blocks:
                if getattr(block, "type", None) == "tool_use":
                    tool_calls_made += 1
                    result = _dispatch_tool(
                        block.name, dict(block.input or {}), tools
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result.get("content", ""),
                            "is_error": bool(result.get("is_error", False)),
                        }
                    )
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in content_blocks],
                }
            )
            messages.append({"role": "user", "content": tool_results})
            raw_messages.append({"role": "user", "content": tool_results})
            continue

        # No tool_use → terminal. Extract placeholder output if present.
        text = _extract_text(content_blocks)
        output = _parse_placeholder_output(text)
        if output is not None and output.get("status") == "placeholder":
            status = "placeholder"
        elif stop_reason == "refusal":
            status = "refused"
        elif output is None:
            status = "invalid"
        break

    wallclock_ms = int((time.monotonic() - start) * 1000)
    # cost_paise accrues even on terminated runs (hard rule, VT-35 brief):
    # the API spend already happened; refunds are not a thing.
    cost_paise = compute_cost_paise(
        model=model,
        input_tokens=input_tokens_used,
        output_tokens=output_tokens_used,
    )
    tokens_used = input_tokens_used + output_tokens_used

    return AgentResult(
        status=cast(Any, status),
        terminated_by=None,
        output=output,
        tokens_used=tokens_used,
        tool_calls_made=tool_calls_made,
        wallclock_ms=wallclock_ms,
        cost_paise=cost_paise,
        raw_messages=raw_messages,
        terminated_reason=None,
    )


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Best-effort serialisation of an Anthropic content block to a dict."""
    if hasattr(block, "model_dump"):
        return cast(dict[str, Any], block.model_dump())
    if isinstance(block, dict):
        return block
    return {
        "type": getattr(block, "type", None),
        "text": getattr(block, "text", None),
    }


__all__ = [
    "SalesRecoveryContext",
    "run_sales_recovery_agent",
]
