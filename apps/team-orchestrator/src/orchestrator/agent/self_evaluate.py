"""Self-evaluate gate for the Sales Recovery agent (VT-36 / VT-4.5).

Intercepts the agent's draft ``CampaignPlan`` before it reaches the
orchestrator. Calls the ``self_evaluate`` seam (VT-50 — backlog, not
yet implemented) and enforces a two-revise-then-fail policy:

  - PASS                → ship the draft; self_evaluate_status='passed'
  - REVISE (1st time)   → return feedback to the loop; agent re-drafts
  - REVISE (2nd time)   → ship anyway; self_evaluate_status='failed_after_revisions'

Pillar 8 — structural enforcement
---------------------------------
The gate is wired into ``run_sales_recovery_agent``'s terminal step.
The agent has NO code path to return without going through the gate.
"Bypass" is therefore a malformed agent transcript (no
``self_evaluate`` tool-use block in raw_messages) — the gate detects
and routes through itself anyway, charging the tool-counter exactly
once per evaluation.

Hard-limit precedence (Pillar 1)
--------------------------------
Each gate evaluation counts as one tool call against VT-35's 25-call
budget. The gate increments ``ToolCounter`` BEFORE invoking the seam;
if the increment trips the cap, the hard-limit cancel fires and the
gate returns without calling self_evaluate. Hard limits always
precede gate logic.

VT-50 seam contract
-------------------
``SelfEvaluator`` is a Protocol the VT-50 implementation will satisfy.
Tests use ``FakeSelfEvaluator`` (scripted verdicts). The seam shape is
locked here so VT-50 lands as a drop-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

import yaml

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.agent.limits.tool_counter import ToolCounter
from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    SelfEvaluateStatus,
)


_SELF_EVALUATE_CONFIG = (
    Path(__file__).resolve().parents[3] / "config" / "self_evaluate.yaml"
)


# ----------------------------------------------------------------------------
# Verdict + feedback shape
# ----------------------------------------------------------------------------


class SelfEvaluateOutcome(str, Enum):
    """The seam's binary verdict."""

    PASS = "pass"
    REVISE = "revise"


@dataclass(frozen=True)
class SelfEvaluateFeedback:
    """Structured feedback for the four categories. VT-50 returns
    these; the agent's loop receives them as user-message content on a
    REVISE verdict."""

    schema: str | None = None
    pillar: str | None = None
    consistency: str | None = None
    legal: str | None = None

    def as_messages(self) -> list[dict[str, str]]:
        """Render the non-empty categories as a user-message-friendly
        bullet list. Returns a single message list (one dict)."""
        lines: list[str] = ["self_evaluate REVISE — address each:"]
        if self.schema:
            lines.append(f"- schema: {self.schema}")
        if self.pillar:
            lines.append(f"- pillar: {self.pillar}")
        if self.consistency:
            lines.append(f"- consistency: {self.consistency}")
        if self.legal:
            lines.append(f"- legal: {self.legal}")
        return [{"role": "user", "content": "\n".join(lines)}]


@dataclass(frozen=True)
class SelfEvaluateVerdict:
    """A single seam response. On REVISE the feedback is populated."""

    outcome: SelfEvaluateOutcome
    feedback: SelfEvaluateFeedback | None = None


# ----------------------------------------------------------------------------
# Seam Protocol — VT-50 implements this; tests use FakeSelfEvaluator
# ----------------------------------------------------------------------------


class SelfEvaluator(Protocol):
    """The interface VT-50's self_evaluate tool will satisfy.

    Implementations:
      - VT-50 (backlog): Opus-backed real evaluator.
      - ``FakeSelfEvaluator`` (this module): scripted verdicts for tests.
    """

    def evaluate(
        self, draft: CampaignPlan, criteria: list[str]
    ) -> SelfEvaluateVerdict:
        """Evaluate ``draft`` against ``criteria``. May raise on
        transport / model errors — the gate catches and routes via the
        AGENT_INVALID_OUTPUT failure path."""
        ...


@dataclass
class FakeSelfEvaluator:
    """Scripted evaluator for tests. Consumes ``verdicts`` left-to-right
    on each ``evaluate`` call. If ``raise_on_call`` is set, raises the
    given exception instead — used for the seam-error test."""

    verdicts: list[SelfEvaluateVerdict] = field(default_factory=list)
    raise_on_call: Exception | None = None
    calls: int = 0

    def evaluate(
        self, draft: CampaignPlan, criteria: list[str]
    ) -> SelfEvaluateVerdict:
        self.calls += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if not self.verdicts:
            raise RuntimeError("FakeSelfEvaluator: out of scripted verdicts")
        return self.verdicts.pop(0)


# ----------------------------------------------------------------------------
# Gate outcome — the call-site dispatch payload
# ----------------------------------------------------------------------------


class GateAction(str, Enum):
    """What the loop should do after a gate call."""

    SHIP = "ship"          # gate accepted (passed OR failed_after_revisions)
    RETRY = "retry"        # gate revised; loop must run another turn
    ABORTED = "aborted"    # hard-limit cancel fired during the gate call
    SEAM_ERROR = "error"   # seam raised; route as AGENT_INVALID_OUTPUT


@dataclass
class GateOutcome:
    """Result of one gate evaluation. The loop reads ``action`` to
    decide whether to ship, retry, or surface a failure."""

    action: GateAction
    self_evaluate_status: SelfEvaluateStatus = SelfEvaluateStatus.NOT_YET_EVALUATED
    feedback_messages: list[dict[str, str]] = field(default_factory=list)
    error_message: str | None = None


# ----------------------------------------------------------------------------
# Config — two-revise-then-fail (Type 2 to change)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Loaded from ``config/self_evaluate.yaml``. Raising
    ``max_revisions`` is Type 2 governance — does the gate help, or is
    the model simply slow to converge? (CL: VT-36 brief.)"""

    max_revisions: int

    @classmethod
    def load(cls) -> "GateConfig":
        with open(_SELF_EVALUATE_CONFIG, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        max_revisions = int(data.get("max_revisions", 2))
        return cls(max_revisions=max_revisions)


# ----------------------------------------------------------------------------
# Evaluation criteria — the four passed to self_evaluate (Pillar 7/8)
# ----------------------------------------------------------------------------


# Fazal personally reviews and approves these four. Docs are at
# docs/team/self_evaluate_criteria.md.
EVALUATION_CRITERIA: list[str] = [
    "schema",       # Draft is valid CampaignPlan JSON; all validators pass.
    "pillar",       # No invented numbers, no per-vertical heuristics, no
                    # overstated confidence, no retention pressure.
    "consistency",  # Targeting matches messaging; ARRR plausible vs cohort.
    "legal",        # No prohibited content; no misleading financial claims.
]


# ----------------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------------


class SelfEvaluateGate:
    """Two-revise-then-fail gate around ``self_evaluate``.

    Per-invocation state: ``revisions_used``. The gate is constructed
    once per ``run_sales_recovery_agent`` call. Each ``run`` call is
    one evaluation; the loop calls back into the gate until the gate
    returns a SHIP action.
    """

    def __init__(
        self,
        evaluator: SelfEvaluator,
        ctx: CancellationContext,
        tool_counter: ToolCounter,
        config: GateConfig | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.ctx = ctx
        self.tool_counter = tool_counter
        self.config = config or GateConfig.load()
        self.revisions_used = 0
        self.evaluator_calls = 0

    def run(self, draft: CampaignPlan) -> GateOutcome:
        """Evaluate ``draft``. Returns the action the caller must take.

        Tool-counter accounting (VT-35 precedence): increment BEFORE
        invoking the seam. If the increment trips the hard-limit cap,
        the cancellation context is signalled and the gate returns
        without calling the seam — hard limits precede gate logic.
        """
        self.tool_counter.record_dispatch()
        if self.ctx.is_cancelled:
            return GateOutcome(action=GateAction.ABORTED)

        self.evaluator_calls += 1
        try:
            verdict = self.evaluator.evaluate(draft, EVALUATION_CRITERIA)
        except Exception as exc:  # noqa: BLE001 — surface via outcome
            return GateOutcome(
                action=GateAction.SEAM_ERROR,
                error_message=str(exc),
            )

        if verdict.outcome is SelfEvaluateOutcome.PASS:
            return GateOutcome(
                action=GateAction.SHIP,
                self_evaluate_status=SelfEvaluateStatus.PASSED,
            )

        # REVISE — increment FIRST, then decide. Two-revise-then-fail
        # means the Nth REVISE (where N == max_revisions) ships with the
        # failure flag; the agent does not get another redraft.
        self.revisions_used += 1
        if self.revisions_used >= self.config.max_revisions:
            return GateOutcome(
                action=GateAction.SHIP,
                self_evaluate_status=SelfEvaluateStatus.FAILED_AFTER_REVISIONS,
            )

        feedback = verdict.feedback or SelfEvaluateFeedback()
        return GateOutcome(
            action=GateAction.RETRY,
            feedback_messages=feedback.as_messages(),
        )


__all__ = [
    "EVALUATION_CRITERIA",
    "FakeSelfEvaluator",
    "GateAction",
    "GateConfig",
    "GateOutcome",
    "SelfEvaluateFeedback",
    "SelfEvaluateGate",
    "SelfEvaluateOutcome",
    "SelfEvaluateVerdict",
    "SelfEvaluator",
]
