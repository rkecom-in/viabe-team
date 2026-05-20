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
from pydantic import BaseModel, ConfigDict, Field  # noqa: F401 — Field used in input schema

from orchestrator.agent.self_evaluate import (
    SelfEvaluateFeedback,
    SelfEvaluateOutcome,
    SelfEvaluateVerdict,
    SelfEvaluator,
)
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
    """Per-category feedback. Each field: ``None`` (category passed) or
    a critique string (category flagged). pydantic v2 doesn't reserve
    ``schema`` as a model method (it's ``model_json_schema`` now), so
    using the literal category name here is safe. ``protected_namespaces``
    cleared to silence the legacy-shadow warning."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    schema: str | None = None  # type: ignore[assignment]  # noqa: A003 — category name dictated by VT-36 Protocol; shadows BaseModel.schema (deprecated v1 method)
    pillar: str | None = None
    consistency: str | None = None
    legal: str | None = None


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
        client = self._make_client()
        model = _resolve_self_evaluate_model()
        system_prompt = _load_prompt()

        user_payload = {
            "draft_campaign_plan": inputs.draft_campaign_plan,
            "context_summary": inputs.context_summary,
            "attempt_number": inputs.attempt_number,
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
        outcome_str = data.get("outcome")
        feedback_dict = data.get("feedback") or {}

        outcome = (
            SelfEvaluateOutcome.PASS
            if outcome_str == "pass"
            else SelfEvaluateOutcome.REVISE
        )
        feedback_obj = SelfEvaluateFeedback(
            schema=feedback_dict.get("schema"),
            pillar=feedback_dict.get("pillar"),
            consistency=feedback_dict.get("consistency"),
            legal=feedback_dict.get("legal"),
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
