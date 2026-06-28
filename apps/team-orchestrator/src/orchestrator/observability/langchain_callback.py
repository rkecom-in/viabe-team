"""VT-125 OrchestratorReasoningCallback — VT-182 equivalent for langchain agents.

VT-182's ``@with_reasoning_capture`` decorator wraps direct
``client.messages.create`` calls (sales_recovery's ``_run_one_turn``).
The orchestrator-agent uses langchain's ``ChatAnthropic`` wrapper — that
path doesn't reach VT-182's decorator. This module fills the gap:
``OrchestratorReasoningCallback`` is a ``langchain_core.callbacks.BaseCallbackHandler``
that fires on every LLM start/end + tool start, calls VT-180's
``write_step('agent_reasoning_step', ...)`` and also feeds usage
tracking into the ``OrchestratorAgentDriver`` for hard-limit
enforcement (VT-125 Q2 + Q3 Option A — Cowork plan-review locked).

Per CL-220: agent-decision tool calls land as ``agent_reasoning_step``
envelope rows in pipeline_steps.
Per CL-417: canonical columns (parent_step_id, tokens_input,
tokens_output, status, model_used, decision_rationale, step_name)
populated from per-field args.

ContextVar discipline (VT-181 pattern): the callback reads
``_observability_context`` for run_id/tenant_id. Without it, the
callback logs + skips write_step (best-effort per CL-122).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from orchestrator.agent.cost import compute_cost_paise
from orchestrator.observability.decorators import _observability_context
from orchestrator.observability.pii import redact_for_log
from orchestrator.observability.pipeline_observability import write_step

if TYPE_CHECKING:
    from orchestrator.agent.orchestrator_agent_driver import (
        OrchestratorAgentDriver,
        OrchestratorUsage,
    )

logger = logging.getLogger(__name__)

# Anthropic returns date-suffixed model ids (`claude-opus-4-7-20251022`);
# RATES table uses base alias. Same pattern as VT-182 agent_callback.
_MODEL_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _normalize_model_for_rates(model: str) -> str:
    return _MODEL_DATE_SUFFIX_RE.sub("", model)


class OrchestratorReasoningCallback(BaseCallbackHandler):
    """langchain callback bridging orchestrator-agent LLM/tool boundaries
    to VT-180 write_step + VT-125 driver hard-limit tracking.

    Fires on:
      - ``on_llm_end``: capture tokens/usage; tick driver mid-invocation
        check; write ``agent_reasoning_step`` row via write_step.
      - ``on_tool_start``: increment tool_calls counter; tick driver
        mid-invocation check (catches the 6th call BEFORE the tool runs).
      - ``on_llm_error`` / ``on_chain_error``: log + write step with
        status='failed' if context permits.
    """

    def __init__(
        self,
        *,
        driver: "OrchestratorAgentDriver",
        usage: "OrchestratorUsage",
        run_id: UUID,
        tenant_id: UUID,
    ) -> None:
        super().__init__()
        self.driver = driver
        self.usage = usage
        self.run_id = run_id
        self.tenant_id = tenant_id

    # -- llm boundary ------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        # Mid-invocation pre-LLM check (catches the case where prior
        # boundary pushed us over a limit; we cancel before incurring
        # another LLM cost).
        self.driver.check_mid_invocation(
            self.usage, run_id=self.run_id, tenant_id=self.tenant_id
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage_data = self._extract_usage(response)
        if usage_data:
            self.usage.tokens_input += usage_data.get("input_tokens", 0)
            self.usage.tokens_output += usage_data.get("output_tokens", 0)
            model = usage_data.get("model")
            if model:
                normalized = _normalize_model_for_rates(model)
                try:
                    incremental_cost = compute_cost_paise(
                        model=normalized,
                        input_tokens=usage_data.get("input_tokens", 0),
                        output_tokens=usage_data.get("output_tokens", 0),
                    )
                    self.usage.cost_paise += incremental_cost
                    # VT-193 fix: propagate the per-step cost into the
                    # write_step row. Prior VT-125 shape only updated
                    # ``self.usage.cost_paise`` cumulatively; the
                    # ``write_step`` row received cost_paise=0 because
                    # the dict had no ``cost_paise`` key. pipeline_runs.
                    # total_cost_paise sums these per-step values, so
                    # the bug silently zeroed every brain-wired run's
                    # cost reporting (surfaced by sprint1_e2e_smoke.py
                    # A3 + vt193_brain_wiring A2 / A6).
                    usage_data["cost_paise"] = incremental_cost
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "VT-125 cost computation skipped",
                        extra={
                            "model": model,
                            "normalized": normalized,
                            "exc": repr(exc),
                        },
                    )

        self._write_reasoning_step(response, usage_data, status="completed")

        # Post-LLM mid-invocation check (catches token/cost overshoot
        # from the call we just completed).
        self.driver.check_mid_invocation(
            self.usage, run_id=self.run_id, tenant_id=self.tenant_id
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.warning(
            "OrchestratorReasoningCallback on_llm_error",
            extra={
                "error": repr(error),
                "run_id": str(self.run_id),
                "tenant_id": str(self.tenant_id),
            },
        )

    # -- tool boundary -----------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        self.usage.tool_calls += 1
        self.driver.check_mid_invocation(
            self.usage, run_id=self.run_id, tenant_id=self.tenant_id
        )

    # -- helpers -----------------------------------------------------

    def _extract_usage(self, response: Any) -> dict[str, Any]:
        """Pull tokens/model from langchain's LLMResult shape.

        langchain wraps the Anthropic Messages SDK response in an
        ``LLMResult`` with ``llm_output`` carrying usage metadata.
        Different langchain versions land usage in slightly different
        places; this method scans the common surfaces.
        """
        out: dict[str, Any] = {}

        # Newer langchain: response.llm_output['usage']
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            usage = llm_output.get("usage", {})
            if isinstance(usage, dict):
                out["input_tokens"] = int(usage.get("input_tokens", 0) or 0)
                out["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
                # VT-194 prompt-caching fields. Present when
                # ``cache_control`` markers are set on the system prompt
                # (per orchestrator_agent.ORCHESTRATOR_AGENT_SYSTEM_MESSAGE).
                # First dispatch within TTL: cache_creation_input_tokens > 0.
                # Subsequent dispatches within TTL: cache_read_input_tokens > 0.
                out["cache_creation_input_tokens"] = int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                )
                out["cache_read_input_tokens"] = int(
                    usage.get("cache_read_input_tokens", 0) or 0
                )
            model = llm_output.get("model_name") or llm_output.get("model")
            if model:
                out["model"] = model

        # Per-generation: response.generations[0][0].message.usage_metadata
        if not out.get("input_tokens"):
            try:
                gens = getattr(response, "generations", [])
                if gens and gens[0]:
                    first = gens[0][0]
                    msg = getattr(first, "message", None)
                    if msg is not None:
                        usage_md = getattr(msg, "usage_metadata", None)
                        if isinstance(usage_md, dict):
                            out["input_tokens"] = int(
                                usage_md.get("input_tokens", 0) or 0
                            )
                            out["output_tokens"] = int(
                                usage_md.get("output_tokens", 0) or 0
                            )
                            # VT-194: also scan usage_metadata for cache
                            # fields when the primary llm_output surface
                            # didn't carry them.
                            if not out.get("cache_creation_input_tokens"):
                                input_details = usage_md.get("input_token_details", {})
                                if isinstance(input_details, dict):
                                    out["cache_creation_input_tokens"] = int(
                                        input_details.get("cache_creation", 0) or 0
                                    )
                                    out["cache_read_input_tokens"] = int(
                                        input_details.get("cache_read", 0) or 0
                                    )
                        response_md = getattr(msg, "response_metadata", None)
                        if isinstance(response_md, dict) and "model" not in out:
                            model = response_md.get("model_name") or response_md.get(
                                "model"
                            )
                            if model:
                                out["model"] = model
            except Exception:  # noqa: BLE001
                pass

        return out

    def _write_reasoning_step(
        self,
        response: Any,
        usage_data: dict[str, Any],
        *,
        status: str,
    ) -> None:
        ctx = _observability_context.get()
        if ctx is None:
            logger.warning(
                "VT-125 callback skipping write — no ObservabilityContext",
                extra={"run_id": str(self.run_id), "tenant_id": str(self.tenant_id)},
            )
            return

        think_text = self._first_text(response)
        think_text_redacted: str | None = None
        if think_text:
            redacted = redact_for_log({"text": think_text})
            if isinstance(redacted, dict):
                think_text_redacted = redacted.get("text")

        try:
            write_step(
                step_kind="agent_reasoning_step",
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                step_name="orchestrator_agent_turn",
                input_envelope={
                    # VT-464 D4: prompt_token_count is a REQUIRED field on
                    # AgentReasoningStepInput (extra="forbid"). The LIVE brain
                    # path runs through THIS langchain callback (step_name
                    # 'orchestrator_agent_turn'), not agent_callback — it
                    # previously omitted prompt_token_count, so every deployed
                    # brain reasoning-step envelope soft-failed validation
                    # (payload_validation_failed=True) and Ops replay degraded.
                    # The prompt (input) token count is the same source
                    # agent_callback uses: this turn's response usage
                    # input_tokens (extracted into usage_data above).
                    "prompt_token_count": int(
                        usage_data.get("input_tokens", 0) or 0
                    ),
                    "context_bundle_hash": "<langchain-passthrough>",
                    "context_bundle_components": [],
                    "context_bundle_token_count": 0,
                    "prior_tool_calls_count": self.usage.tool_calls,
                    "prior_tool_calls_summary": [],
                },
                output_envelope={
                    "think_text": think_text_redacted,
                    "action": None,
                    "action_args": None,
                    "logfire_trace_id": None,
                    # VT-194 prompt-caching observability — surfaces per-step
                    # cache creation/read so the canary + Ops Console can
                    # report cache effectiveness.
                    "cache_creation_input_tokens": int(
                        usage_data.get("cache_creation_input_tokens", 0) or 0
                    ),
                    "cache_read_input_tokens": int(
                        usage_data.get("cache_read_input_tokens", 0) or 0
                    ),
                },
                decision_rationale=(
                    think_text_redacted[:400] if think_text_redacted else None
                ),
                parent_step_id=ctx.parent_step_id,
                status=status,
                cost_paise=int(usage_data.get("cost_paise", 0) or 0),
                model_used=usage_data.get("model"),
                tokens_input=int(usage_data.get("input_tokens", 0) or 0),
                tokens_output=int(usage_data.get("output_tokens", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "VT-125 callback write_step swallowed (CL-122 best-effort)",
                extra={"exc": repr(exc)},
            )

    def _first_text(self, response: Any) -> str | None:
        try:
            gens = getattr(response, "generations", [])
            if gens and gens[0]:
                first = gens[0][0]
                text = getattr(first, "text", None)
                if isinstance(text, str) and text:
                    return text
                msg = getattr(first, "message", None)
                if msg is not None:
                    content = getattr(msg, "content", None)
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                return block.get("text")
        except Exception:  # noqa: BLE001
            pass
        return None


__all__ = ["OrchestratorReasoningCallback"]
