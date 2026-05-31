"""self_evaluate MCP tool — Opus-backed quality gate (VT-50).

Fills the ``SelfEvaluator`` Protocol VT-36 (PR #35) left open. The tool
subclasses the VT-39 ``MCPTool`` framework; an adapter exposes the
Protocol-shaped ``evaluate(draft, criteria) -> SelfEvaluateVerdict``
the gate calls.

Two contracts, one tool
-----------------------
- ``SelfEvaluateTool`` is the framework's contract. ``execute(ctx,
  inputs) -> SelfEvaluateOutput`` runs the Opus call; the framework's
  ``call(ctx, raw_inputs)`` wraps it with input/output validation,
  telemetry, and an error envelope.

- ``SelfEvaluateAdapter`` is the gate's contract. Constructed once per
  run with a live ``ToolContext``; its ``evaluate(draft, criteria)``
  routes through ``tool.call`` and unpacks the result into the
  Protocol-shaped ``SelfEvaluateVerdict``. Exceptions from the tool
  layer propagate (the gate already wraps them as ``GateAction.SEAM_ERROR``
  → AGENT_INVALID_OUTPUT routing, CL-265).

Contract reconciliation (CL-265)
-------------------------------
VT-36's Protocol return type wins: ``SelfEvaluateVerdict`` carries
``outcome`` ∈ {PASS, REVISE} and ``SelfEvaluateFeedback`` with four
``str | None`` fields. The 2026-05-04 page's richer schema
(per-category ``CategoryFeedback``, ``overall_severity``,
``model_used``/``tokens_used``/``cost_paise`` on the return) is CUT
from the return type. The richer telemetry STILL lands — through the
framework's ``ToolResult`` (tokens_used, cost_paise, latency_ms) and
the ``pipeline_steps`` row written by the production telemetry sink.

Independence (Pillar 7)
-----------------------
The input schema accepts only ``draft_campaign_plan`` +
``context_summary`` + ``attempt_number``. No ``reasoning_chain`` field;
the schema rejects extras (``extra='forbid'``), so an agent that tries
to pass its reasoning gets a validation error — tested.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agent.self_evaluate import (
    SelfEvaluateFeedback,
    SelfEvaluateOutcome,
    SelfEvaluateVerdict,
    SelfEvaluator,
)
from orchestrator.observability.decorators import tool_step
from team_shared.mcp import (
    MCPTool,
    ToolContext,
    ToolResult,
    ToolStatus,
)


# ---------------------------------------------------------------------------
# Constants — prompt path + token budgets
# ---------------------------------------------------------------------------


_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "self_evaluate_v1.md"
)

# Per-response output cap for the seam's Opus call. The verdict JSON is
# small (~150 tokens); 1024 is generous headroom.
_MAX_OUTPUT_TOKENS = 1024

# Models config path — the tool reads the model pin from here (Pillar 8:
# never hardcode a model string). ``self_evaluate.py`` lives at
# ``src/orchestrator/agent/tools/self_evaluate.py``, four parents deep
# from ``apps/team-orchestrator/``; ``config/models.yaml`` sits beside
# ``src/``.
_MODELS_YAML = (
    Path(__file__).resolve().parents[4] / "config" / "models.yaml"
)

# Markdown code-fence stripper — Opus occasionally wraps JSON in a
# fence even when the prompt forbids it (cf. VT-32 canary failure #3).
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pydantic schemas — input / output
# ---------------------------------------------------------------------------


class SelfEvaluateInput(BaseModel):
    """Input contract. ``tenant_id`` lives on ToolContext (Pillar 3 —
    the VT-39 framework refuses any input schema declaring it).

    ``reasoning_chain`` is INTENTIONALLY absent and the schema is
    ``extra='forbid'`` — an agent that tries to pass its reasoning
    gets an INVALID_INPUT envelope (Pillar 7: the evaluator sees only
    the final draft + compact context)."""

    model_config = ConfigDict(extra="forbid")

    draft_campaign_plan: dict[str, Any]
    context_summary: dict[str, Any] = Field(default_factory=dict)
    attempt_number: int = Field(default=1, ge=1, le=2)


class _FeedbackPayload(BaseModel):
    """Per-category feedback. Each field: ``None`` / empty list
    (category passed) or a LIST of distinct critique strings (category
    flagged).

    Wire-format note: the JSON the model emits uses keys ``schema``,
    ``pillar``, ``consistency``, ``legal`` (matches VT-36's
    ``SelfEvaluateFeedback``). The Python attribute ``schema`` would
    shadow pydantic's deprecated ``BaseModel.schema`` v1 method, so the
    attribute is ``schema_critique`` with ``alias='schema'``.
    ``populate_by_name=True`` accepts both spellings on validation;
    dump-by-alias is used at the call site so the wire key stays
    ``schema`` in ``ToolResult.data``.

    v1.1 (VT-SalesRecovery-Agent wiring): widened from
    ``str | None`` to ``list[str] | None`` so multiple distinct
    violations within one category are preserved end-to-end. The
    prompt instructs the model to emit one entry per distinct
    violation, never a summary string.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_critique: list[str] | None = Field(default=None, alias="schema")
    pillar: list[str] | None = None
    consistency: list[str] | None = None
    legal: list[str] | None = None


class SelfEvaluateOutput(BaseModel):
    """Output contract for the framework. Maps cleanly to the
    Protocol-shaped ``SelfEvaluateVerdict`` via the adapter."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["pass", "revise"]
    feedback: _FeedbackPayload


# ---------------------------------------------------------------------------
# Model + prompt loading
# ---------------------------------------------------------------------------


def _resolve_self_evaluate_model() -> str:
    """Read the model pin from config/models.yaml. ``VIABE_ENV=production``
    selects the ``production`` slot; anything else selects ``test``
    (Haiku canary). Brief item 6: NO hardcoded model string."""
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return cast(str, config["self_evaluate"][slot])


def _load_prompt() -> str:
    """Read the v1 prompt verbatim. The HTML-comment metadata header
    at the top is sent to the model as-is — ~10 tokens of overhead,
    well within the 3000-token budget."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Observability-wrapped impl (VT-190)
# ---------------------------------------------------------------------------
#
# VT-181 deferred the @tool_step retrofit because ``SelfEvaluateTool`` is an
# MCPTool subclass: ``execute(self, ctx, inputs)`` binds to ``{self, ctx,
# inputs}`` under ``inspect.signature.bind``, not the input model's fields, so
# the decorator's ``envelope_in.model_validate`` could never see the real
# args. VT-190 resolves this with the row's Option A: extract the execute body
# into a module-level function whose KEYWORD signature mirrors
# ``SelfEvaluateInput`` exactly, then decorate THAT. ``sig.bind`` now yields
# ``{draft_campaign_plan, context_summary, attempt_number}`` — clean
# validation against ``envelope_in=SelfEvaluateInput``. This is the same
# pattern used by the L0 memory tools in ``orchestrator_agent.py`` (the impl
# is plain; the decorator wraps it; the MCPTool/langchain layer calls the
# wrapped function).
#
# step_kind is ``mcp_tool_call`` (the generic tool-call kind, matching
# ``compose_owner_output_tool``), NOT ``self_evaluate_gate``. The
# ``self_evaluate_gate`` step_kind is owned by the gate-level emitter
# ``sales_recovery._emit_self_evaluate_gate``, which records per-gate-attempt
# verdict + RETRY/REJECT telemetry the tool layer cannot see (the tool only
# sees one Opus call's args/result). Routing the tool's ``{outcome, feedback}``
# payload through the ``self_evaluate_gate`` envelope would soft-fail
# write_step validation (that envelope is ``{verdict, reasons}`` /
# ``extra='forbid'``). ``mcp_tool_call`` wraps args/result into the canonical
# ``{tool_name, tool_args}`` / ``{tool_result, cost_paise, duration_ms}`` shape
# the decorator already constructs.


@tool_step(
    step_kind="mcp_tool_call",
    envelope_in=SelfEvaluateInput,
    envelope_out=SelfEvaluateOutput,
    step_name="self_evaluate",
)
def _self_evaluate_impl(
    *,
    draft_campaign_plan: dict[str, Any],
    context_summary: dict[str, Any],
    attempt_number: int,
) -> SelfEvaluateOutput:
    """Run the Opus quality-gate evaluation for one draft.

    Keyword-only signature mirrors ``SelfEvaluateInput`` so the
    ``@tool_step`` decorator's ``inspect.signature.bind`` produces a dict
    that validates against ``envelope_in=SelfEvaluateInput``. The model pin
    and Anthropic client are resolved INSIDE the impl (dependency resolution,
    not bound args) — keeping them off the signature so they never pollute
    the observability envelope, mirroring how the L0 tools resolve
    ``get_pool()`` internally.

    The client is constructed via ``SelfEvaluateTool._make_client`` so the
    test injection seam (``classmethod`` patched on the type) keeps working.
    """
    client = SelfEvaluateTool._make_client()
    model = _resolve_self_evaluate_model()
    system_prompt = _load_prompt()

    user_payload = {
        "draft_campaign_plan": draft_campaign_plan,
        "context_summary": context_summary,
        "attempt_number": attempt_number,
    }
    # mypy: anthropic Messages.create's overloads are TypedDict-heavy
    # — typing the plain-dict messages list to match would add
    # noise without value (same precedent as sales_recovery.py).
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[
            {"role": "user", "content": json.dumps(user_payload)},
        ],
    )

    # Extract text content from the assistant turn. Anthropic's
    # response.content is a list of blocks; we want concatenated
    # text only.
    raw_text = ""
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            raw_text += text
    raw_text = raw_text.strip()

    # Tolerate a markdown code-fence wrapper.
    fence_match = _CODE_FENCE_RE.match(raw_text)
    if fence_match is not None:
        raw_text = fence_match.group("body").strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"self_evaluate Opus emitted unparseable JSON: {exc}"
        ) from exc

    # SelfEvaluateOutput validation enforces the contract; the
    # framework's output-validation layer would otherwise catch
    # this, but parsing it here gives a clearer error path.
    return SelfEvaluateOutput.model_validate(parsed)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class SelfEvaluateTool(MCPTool[SelfEvaluateInput, SelfEvaluateOutput]):
    """Opus-backed quality gate. Receives a draft + compact context,
    returns a per-category PASS/REVISE verdict.

    Pillar 7 — independence: the input contract REJECTS a
    ``reasoning_chain`` field; the evaluator sees only the final draft.

    Pillar 2 — rare LLM-backed tool. Rationale lives in
    docs/team/llm-backed-tools-rationale.md (locked v1 LLM-backed set
    per VT-39 / CL-265): semantic cases the Pydantic model can't
    catch; Opus over Sonnet/Haiku because false negatives erode owner
    trust irrecoverably; per-eval cost ~₹10-15 within budget.

    VT-190 retrofit (Option A): the Opus call body now lives in the
    module-level ``_self_evaluate_impl`` wrapped by ``@tool_step`` so each
    invocation writes a ``mcp_tool_call`` pipeline_steps row via VT-180's
    write_step (uniform with ``compose_owner_output_tool`` + the L0 tools).
    ``execute`` is a thin shim that forwards the validated input-model fields
    to the wrapped impl. The MCPTool ``execute(self, ctx, inputs)`` signature
    that VT-181 could not bind is sidestepped: the decorator wraps the plain
    keyword-only impl, not the bound method.

    The gate-level emitter ``sales_recovery._emit_self_evaluate_gate`` is
    RETAINED as a compatibility shim — it records per-gate-attempt
    (attempt_number + RETRY/REJECT verdict) telemetry under step_kind
    ``self_evaluate_gate``, which is a different granularity than the
    per-Opus-call ``mcp_tool_call`` row this decorator now writes.
    """

    name = "self_evaluate"
    description = (
        "Opus-backed evaluator. Receives a draft CampaignPlan + compact "
        "context; returns a per-category PASS/REVISE verdict (schema, "
        "pillar, consistency, legal). Cannot be bypassed by the agent."
    )
    input_schema = SelfEvaluateInput
    output_schema = SelfEvaluateOutput

    @classmethod
    def is_llm_backed(cls) -> bool:
        # Rationale: docs/team/llm-backed-tools-rationale.md — Locked
        # LLM-backed tools, self_evaluate entry. Opus chosen because
        # false negatives on quality-gate verdicts cost owner trust
        # irrecoverably; semantic critique categories cannot be
        # implemented deterministically without an unreasonable rule
        # explosion. Cost ~₹10-15/eval, within budget.
        return True

    # Injection seam — tests patch this to a MagicMock; production
    # constructs a real client. ``classmethod`` so the class can be
    # patched on the type itself (not per-instance).
    @classmethod
    def _make_client(cls) -> Anthropic:
        return Anthropic()

    def execute(
        self,
        ctx: ToolContext,
        inputs: SelfEvaluateInput,
    ) -> SelfEvaluateOutput:
        # Thin shim over the @tool_step-wrapped impl (VT-190). The
        # decorator reads the ``observability_context`` ContextVar to write
        # one mcp_tool_call pipeline_steps row per invocation; absent the
        # ContextVar it logs + skips (best-effort, CL-122). Forward the
        # validated input-model fields as keyword args so the decorator's
        # signature.bind validates them against envelope_in=SelfEvaluateInput.
        return _self_evaluate_impl(
            draft_campaign_plan=inputs.draft_campaign_plan,
            context_summary=inputs.context_summary,
            attempt_number=inputs.attempt_number,
        )


def _dump_output_with_wire_keys(output: SelfEvaluateOutput) -> dict[str, Any]:
    """Dump ``SelfEvaluateOutput`` keeping the wire key ``schema`` (not
    the Python attribute ``schema_critique``). Used by the adapter so
    ``ToolResult.data`` matches the model-emitted wire format."""
    return output.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# Protocol adapter — bridges the tool to VT-36's SelfEvaluator
# ---------------------------------------------------------------------------


class SelfEvaluateAdapter:
    """Wraps ``SelfEvaluateTool`` to expose the VT-36 ``SelfEvaluator``
    Protocol the gate consumes.

    Constructed once per orchestrator run with a live ``ToolContext``
    (tenant + run identity). The gate calls ``adapter.evaluate(draft,
    criteria)`` per-attempt; the adapter routes through ``tool.call``
    (full framework lifecycle) and unpacks the resulting ToolResult.

    On framework-level errors (INVALID_INPUT / INVALID_OUTPUT /
    EXECUTION_ERROR), the adapter RAISES so the gate's existing
    SEAM_ERROR branch routes the failure via the error router
    (AGENT_INVALID_OUTPUT). The gate already handles this path; the
    adapter only converts the framework's structured envelope into a
    Python exception with the envelope's message.
    """

    def __init__(
        self,
        ctx: ToolContext,
        attempt_number: int = 1,
        tool: SelfEvaluateTool | None = None,
    ) -> None:
        self._ctx = ctx
        self._tool = tool or SelfEvaluateTool()
        self.attempt_number = attempt_number

    def evaluate(
        self,
        draft: Any,
        criteria: list[str],
    ) -> SelfEvaluateVerdict:
        """Run the tool through the framework lifecycle and pack the
        result into a ``SelfEvaluateVerdict``.

        ``criteria`` is accepted for Protocol conformance but not
        forwarded to the seam — the four categories are baked into
        the system prompt at v1.0. (The criteria list lives in
        ``orchestrator.agent.self_evaluate.EVALUATION_CRITERIA`` and
        the framework / prompt agree on them.)"""

        draft_dict = (
            draft.model_dump(mode="json") if hasattr(draft, "model_dump") else draft
        )

        raw_inputs: dict[str, Any] = {
            "draft_campaign_plan": draft_dict,
            "context_summary": {},
            "attempt_number": self.attempt_number,
        }

        result: ToolResult = self._tool.call(self._ctx, raw_inputs)

        if result.status is not ToolStatus.OK:
            assert result.error is not None
            raise RuntimeError(
                f"self_evaluate tool failed: code={result.error.code.value} "
                f"message={result.error.message}"
            )

        data = result.data or {}
        # Re-validate the framework's dumped data back into the typed
        # model — gives the adapter direct attribute access without
        # juggling field-name vs. alias on the dict. populate_by_name
        # accepts both the dumped ``schema`` (alias) and the attribute
        # ``schema_critique`` form.
        output = SelfEvaluateOutput.model_validate(data)

        outcome = (
            SelfEvaluateOutcome.PASS
            if output.outcome == "pass"
            else SelfEvaluateOutcome.REVISE
        )
        feedback_obj = SelfEvaluateFeedback(
            schema=list(output.feedback.schema_critique)
            if output.feedback.schema_critique
            else None,
            pillar=list(output.feedback.pillar) if output.feedback.pillar else None,
            consistency=list(output.feedback.consistency)
            if output.feedback.consistency
            else None,
            legal=list(output.feedback.legal) if output.feedback.legal else None,
        )
        # On PASS we elide the feedback dataclass (None) — the
        # gate's branching treats `None` as "no feedback to surface".
        if outcome is SelfEvaluateOutcome.PASS:
            return SelfEvaluateVerdict(outcome=outcome, feedback=None)
        return SelfEvaluateVerdict(outcome=outcome, feedback=feedback_obj)


# ---------------------------------------------------------------------------
# Registration — wire into the central registry at import time
# ---------------------------------------------------------------------------


def _register() -> None:
    # Imported here to avoid a cycle at module load when the registry
    # imports its consumers.
    from orchestrator.agent import tool_registry

    tool_registry.register(SelfEvaluateTool)


_register()


__all__ = [
    "SelfEvaluateAdapter",
    "SelfEvaluateInput",
    "SelfEvaluateOutput",
    "SelfEvaluateTool",
]


# Sanity static-type check: the adapter satisfies the VT-36 Protocol.
# Runtime cost: nothing — the assignment is type-only.
_PROTOCOL_CONFORMANCE_CHECK: type[SelfEvaluator] = SelfEvaluateAdapter
