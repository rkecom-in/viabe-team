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

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

import yaml

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.agent.limits.tool_counter import ToolCounter
from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    CampaignPlanProposed,
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


class GradeTier(str, Enum):
    """VT-500 — the grounding tier a draft is graded under.

    The grading itself (the four-category Opus grade) is IDENTICAL for
    both tiers. The tier governs exactly ONE post-grade behaviour: on the
    ``SIMPLE`` tier the gate drops ``expected_arrr``-path critiques (the
    ROI/ARRR business-justification axis) before the PASS/REVISE
    decision, and the prompt receives a cooperative ``grade_tier`` hint
    not to raise them. Every other critique — schema, the non-ARRR pillar
    rules, cohort-grounding consistency, legal, anti-fabrication, PII —
    stays binding on BOTH tiers. ``STRICT`` is the default and the
    byte-identical pre-VT-500 behaviour (no critique dropped).
    """

    STRICT = "strict"
    SIMPLE = "simple"


@dataclass(frozen=True)
class SelfEvaluateFeedback:
    """Structured feedback for the four categories. VT-50 returns
    these; the agent's loop receives them as user-message content on a
    REVISE verdict.

    v1.1 (VT-SalesRecovery-Agent wiring): each category is a LIST of
    distinct critique strings, NOT a single string. Multiple violations
    within one category (e.g. two invented numbers under ``pillar``)
    are preserved end-to-end instead of collapsing to one summary. A
    category that passed evaluation carries ``None`` (or an empty list
    — treated equivalently by ``as_messages``); a category that flagged
    carries one OR more critique entries.
    """

    schema: list[str] | None = None
    pillar: list[str] | None = None
    consistency: list[str] | None = None
    legal: list[str] | None = None

    def as_messages(self) -> list[dict[str, str]]:
        """Render the non-empty categories as a user-message-friendly
        bullet list. One bullet per distinct violation. Returns a single
        message list (one dict)."""
        lines: list[str] = ["self_evaluate REVISE — address each:"]
        for category in ("schema", "pillar", "consistency", "legal"):
            entries = getattr(self, category) or []
            for entry in entries:
                lines.append(f"- {category}: {entry}")
        return [{"role": "user", "content": "\n".join(lines)}]

    def is_empty(self) -> bool:
        """True iff no category carries any critique. Used by the VT-500
        simple-tier filter: after dropping ``expected_arrr``-path entries,
        an all-empty feedback means there is nothing left to revise → the
        REVISE collapses to PASS."""
        return not (self.schema or self.pillar or self.consistency or self.legal)


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
        self,
        draft: CampaignPlan,
        criteria: list[str],
        *,
        tier: GradeTier = GradeTier.STRICT,
    ) -> SelfEvaluateVerdict:
        """Evaluate ``draft`` against ``criteria``. May raise on
        transport / model errors — the gate catches and routes via the
        AGENT_INVALID_OUTPUT failure path.

        VT-500: ``tier`` is a cooperative hint forwarded to the grader's
        prompt (``grade_tier``). It is defaulted (back-compat: callers and
        fakes that ignore it keep working) and NEVER the source of the
        relaxation's determinism — the gate's deterministic post-grade
        ``expected_arrr`` filter is. An implementation MAY forward it to a
        prompt but MUST NOT relax any non-ARRR rule on its account."""
        ...


@dataclass
class FakeSelfEvaluator:
    """Scripted evaluator for tests. Consumes ``verdicts`` left-to-right
    on each ``evaluate`` call. If ``raise_on_call`` is set, raises the
    given exception instead — used for the seam-error test."""

    verdicts: list[SelfEvaluateVerdict] = field(default_factory=list)
    raise_on_call: Exception | None = None
    calls: int = 0
    last_tier: GradeTier | None = None  # VT-500: the tier the gate passed on the last call

    def evaluate(
        self,
        draft: CampaignPlan,
        criteria: list[str],
        *,
        tier: GradeTier = GradeTier.STRICT,
    ) -> SelfEvaluateVerdict:
        # ``tier`` is accepted for Protocol conformance and RECORDED, but
        # never alters the scripted verdict — VT-500's determinism comes
        # from the gate's post-grade filter, not the evaluator. Tests
        # script an ARRR-only REVISE and assert the GATE collapses it.
        self.calls += 1
        self.last_tier = tier
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

    SHIP = "ship"          # gate accepted; ship the draft as passed
    RETRY = "retry"        # gate revised; loop must run another turn
    ABORTED = "aborted"    # hard-limit cancel fired during the gate call
    SEAM_ERROR = "error"   # seam raised; route as AGENT_INVALID_OUTPUT
    REJECTED = "rejected"  # exhausted retry; route as SELF_EVAL_REJECTED


@dataclass
class GateOutcome:
    """Result of one gate evaluation. The loop reads ``action`` to
    decide whether to ship, retry, or surface a failure.

    ``rejection_feedback`` is populated only when ``action`` is
    ``REJECTED`` — it carries the final REVISE's per-category feedback
    so the loop can include it in the FailureRecord metadata for
    escalation. ``feedback_messages`` is populated on ``RETRY`` only.
    """

    action: GateAction
    self_evaluate_status: SelfEvaluateStatus = SelfEvaluateStatus.NOT_YET_EVALUATED
    feedback_messages: list[dict[str, str]] = field(default_factory=list)
    error_message: str | None = None
    rejection_feedback: SelfEvaluateFeedback | None = None
    attempt_number: int = 1
    outcome: SelfEvaluateOutcome | None = None


# ----------------------------------------------------------------------------
# VT-500 — money_bearing resolve (fail-closed) + the one-directional ARRR filter
# ----------------------------------------------------------------------------


def _resolve_money_bearing(template_id: str) -> bool:
    """Resolve ``money_bearing`` off the template registry, FAIL-CLOSED.

    Mirrors ``agents/l3_hold.py:_template_is_money_bearing`` exactly: an
    unresolvable / drifted / errored template is treated as money-bearing
    (``True``), so it can NEVER satisfy the ``money_bearing is False``
    clause of the simple-tier predicate — it falls to the strict grade.
    A registry import or resolve failure is also money-bearing.
    """
    try:
        from orchestrator.templates_registry import TemplateRegistryError
        from orchestrator.templates_registry import resolve as registry_resolve

        try:
            return bool(registry_resolve(template_id, "en").money_bearing)
        except TemplateRegistryError:
            return True
    except Exception:  # noqa: BLE001 — any resolve/import error ⇒ fail-closed strict
        return True


# A critique is on the ARRR business-justification axis IFF its cited JSON
# path is ``expected_arrr`` itself or a sub-field of it. The prompt mandates
# each critique LEAD with the exact JSON path (self_evaluate_v1.md), so the
# path is the leading token. We anchor at the start (after optional quoting)
# and require the field to be followed by ``.`` (a sub-field, e.g.
# ``expected_arrr.basis``), whitespace, or end-of-string. This is provably
# one-directional: a ``target_cohort`` / ``message_plan`` / ``selection_reason``
# / ``legal`` / ``schema`` / PII / fabrication critique LEADS with a different
# path token and can NEVER match — only ``expected_arrr`` critiques are dropped.
_ARRR_PATH_RE = re.compile(r"^[\s`'\"]*expected_arrr(?:[.\s]|$)")


def _is_arrr_critique(entry: str) -> bool:
    """True iff ``entry``'s cited path is ``expected_arrr`` / ``expected_arrr.*``."""
    return _ARRR_PATH_RE.match(entry) is not None


def _filter_arrr_only(feedback: "SelfEvaluateFeedback") -> "SelfEvaluateFeedback":
    """Return a copy of ``feedback`` with ONLY ``expected_arrr``-path critiques
    dropped, across all four categories. One-directional by construction (see
    ``_ARRR_PATH_RE``): it can strip nothing but ARRR-path entries. A category
    that becomes empty collapses to ``None``; a non-ARRR entry always survives.
    """

    def _drop(entries: list[str] | None) -> list[str] | None:
        if not entries:
            return entries
        kept = [e for e in entries if not _is_arrr_critique(e)]
        return kept or None

    return SelfEvaluateFeedback(
        schema=_drop(feedback.schema),
        pillar=_drop(feedback.pillar),
        consistency=_drop(feedback.consistency),
        legal=_drop(feedback.legal),
    )


# ----------------------------------------------------------------------------
# Config — two-revise-then-fail (Type 2 to change)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Loaded from ``config/self_evaluate.yaml``. Raising
    ``max_revisions`` is Type 2 governance — does the gate help, or is
    the model simply slow to converge? (CL: VT-36 brief.)

    VT-500 adds the ``simple_tier`` knobs (also Type 2):
      - ``simple_tier_enabled`` — the kill-switch. ``False`` reverts the
        gate to all-strict (every draft takes the full grade); a clean
        one-line revert if the calibration misbehaves on dev.
      - ``simple_templates`` — the template allow-list that may enter the
        relaxed lane. Defaults to the imported ``WINBACK_TEMPLATE_NAME``
        (no literal re-hardcoded in this module). The cohort ceiling is
        NOT stored here — it is imported from ``L3_AUTO_MAX_BATCH`` at
        classification time (one source of truth; §4 of the plan).
    """

    max_revisions: int
    simple_tier_enabled: bool = True
    simple_templates: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> "GateConfig":
        with open(_SELF_EVALUATE_CONFIG, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        max_revisions = int(data.get("max_revisions", 2))

        simple_cfg = data.get("simple_tier") or {}
        # Default ENABLED per the plan (a kill ``enabled: false`` reverts).
        simple_enabled = bool(simple_cfg.get("enabled", True))
        templates = simple_cfg.get("templates")
        if not templates:
            # No literal duplicated: fall back to the executor's canonical
            # winback-simple template name.
            from orchestrator.agents.sales_recovery_executor import (
                WINBACK_TEMPLATE_NAME,
            )

            templates = [WINBACK_TEMPLATE_NAME]
        return cls(
            max_revisions=max_revisions,
            simple_tier_enabled=simple_enabled,
            simple_templates=tuple(str(t) for t in templates),
        )


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

    def _classify_tier(self, draft: CampaignPlanProposed) -> GradeTier:
        """VT-500 — classify a PROPOSED draft as SIMPLE or STRICT.

        SIMPLE iff ALL hold (allow-list + fail-closed, NOT a generic
        ``money_bearing == False`` relax):
          1. ``message_plan.template_id`` is in the config allow-list
             (``simple_templates`` — defaults to ``team_winback_simple``).
          2. ``money_bearing is False`` resolved from the registry,
             fail-closed (any resolve error ⇒ money-bearing ⇒ STRICT).
          3. ``target_cohort.cohort_size <= L3_AUTO_MAX_BATCH`` (the bulk
             always-confirm floor — imported, never a literal 20).
        Everything else ⇒ STRICT. The kill-switch (``simple_tier_enabled``
        false) and ANY unexpected error during classification ⇒ STRICT.
        """
        if not self.config.simple_tier_enabled:
            return GradeTier.STRICT
        try:
            template_id = draft.message_plan.template_id
            if template_id not in self.config.simple_templates:
                return GradeTier.STRICT
            # Defence-in-depth beyond the name allow-list: a registry that
            # ever marks this template money-bearing forces strict.
            if _resolve_money_bearing(template_id) is not False:
                return GradeTier.STRICT
            from orchestrator.agents.autonomy import L3_AUTO_MAX_BATCH

            if draft.target_cohort.cohort_size > L3_AUTO_MAX_BATCH:
                return GradeTier.STRICT
            return GradeTier.SIMPLE
        except Exception:  # noqa: BLE001 — classification must fail-closed to STRICT
            return GradeTier.STRICT

    def run(self, draft: CampaignPlan) -> GateOutcome:
        """Evaluate ``draft``. Returns the action the caller must take.

        Tool-counter accounting (VT-35 precedence): increment BEFORE
        invoking the seam. If the increment trips the hard-limit cap,
        the cancellation context is signalled and the gate returns
        without calling the seam — hard limits precede gate logic.

        Locked REVISE contract (VT-SalesRecovery-Agent wiring):
          - PASS first → SHIP with status=PASSED
          - 1st REVISE → RETRY (one retry permitted)
          - 2nd REVISE → REJECTED (no second retry, no ship-with-flag)
        The ``max_revisions`` config value is the threshold at which
        REJECTED fires. With the current ``max_revisions=2``, two
        accumulated REVISE verdicts reject the run.

        VT-491 — variant short-circuit (deterministic, no LLM)
        ------------------------------------------------------
        The grader (and its prompt) is written to grade a ``proposed``
        CampaignPlan ONLY. The other two terminal variants —
        ``out_of_scope`` (a refusal) and ``insufficient_data`` ("no
        campaign possible yet, here's the missing data") — are LEGAL
        terminals with nothing to grade. Handing one to the off-contract
        LLM seam makes its verdict undefined: it PASSed one run and
        REVISEd another (with a factually-wrong critique), and a REVISE
        burns a retry or escalates a legitimate "not enough data"
        terminal to Fazal. So: detect the non-proposed variant FIRST,
        deterministically (``isinstance``, never the LLM), and ACCEPT it
        (SHIP) unchanged — BEFORE ``record_dispatch()`` and before the
        seam. No Opus call happens, so no tool-budget slot is charged
        (``evaluator_calls`` stays 0) and no cost accrues; the plan flows
        on to ``collapse_node`` → ``record_terminal_verdict`` intact (the
        data-remediation terminal that already exists). A real
        ``proposed`` plan still gets the full, unchanged four-category
        grade below — the gate is NOT weakened for real plans.
        """
        if not isinstance(draft, CampaignPlanProposed):
            return GateOutcome(
                action=GateAction.SHIP,
                # Cosmetic for non-proposed terminals: record_terminal_verdict
                # reads ``variant`` + ``missing_data``, never this field. Left
                # at the GateOutcome default (NOT_YET_EVALUATED) — no schema
                # touch. attempt_number=0 marks "no grading attempt".
                attempt_number=0,
                outcome=None,
            )

        # VT-500 — tier classification. Runs AFTER the VT-491 isinstance
        # short-circuit (the draft is now a proven CampaignPlanProposed) and
        # BEFORE record_dispatch. STRICT for every legacy/offer/large draft, so
        # the path below is byte-identical to pre-VT-500 unless the draft is the
        # narrow simple win-back lane.
        tier = self._classify_tier(draft)

        self.tool_counter.record_dispatch()
        if self.ctx.is_cancelled:
            return GateOutcome(action=GateAction.ABORTED)

        self.evaluator_calls += 1
        attempt = self.evaluator_calls
        try:
            verdict = self.evaluator.evaluate(draft, EVALUATION_CRITERIA, tier=tier)
        except Exception as exc:  # noqa: BLE001 — surface via outcome
            return GateOutcome(
                action=GateAction.SEAM_ERROR,
                error_message=str(exc),
                attempt_number=attempt,
            )

        # VT-500 — the ONE relaxation, simple tier ONLY. Run the FULL unchanged
        # grade above; then deterministically drop ONLY ``expected_arrr``-path
        # critiques (the ROI/ARRR defensibility axis) before the PASS/REVISE
        # decision. Provably one-directional (``_filter_arrr_only`` can strip
        # nothing but ARRR-path entries): a fabrication / PII / cohort-grounding
        # / legal / schema critique ALWAYS survives and still fails the draft.
        # If, after the drop, nothing remains, the REVISE collapses to PASS. The
        # STRICT path never enters this block — it is untouched.
        if tier is GradeTier.SIMPLE and verdict.outcome is SelfEvaluateOutcome.REVISE:
            filtered = (
                _filter_arrr_only(verdict.feedback)
                if verdict.feedback is not None
                else None
            )
            if filtered is None or filtered.is_empty():
                verdict = SelfEvaluateVerdict(
                    outcome=SelfEvaluateOutcome.PASS, feedback=None
                )
            else:
                verdict = SelfEvaluateVerdict(
                    outcome=SelfEvaluateOutcome.REVISE, feedback=filtered
                )

        if verdict.outcome is SelfEvaluateOutcome.PASS:
            return GateOutcome(
                action=GateAction.SHIP,
                self_evaluate_status=SelfEvaluateStatus.PASSED,
                attempt_number=attempt,
                outcome=SelfEvaluateOutcome.PASS,
            )

        # REVISE — increment first, then decide between RETRY and REJECT.
        # At max_revisions accumulated, the gate REJECTS the run —
        # never ships a draft known-bad (no ship-with-flag).
        self.revisions_used += 1
        if self.revisions_used >= self.config.max_revisions:
            return GateOutcome(
                action=GateAction.REJECTED,
                self_evaluate_status=SelfEvaluateStatus.FAILED_AFTER_REVISIONS,
                rejection_feedback=verdict.feedback,
                attempt_number=attempt,
                outcome=SelfEvaluateOutcome.REVISE,
            )

        feedback = verdict.feedback or SelfEvaluateFeedback()
        return GateOutcome(
            action=GateAction.RETRY,
            feedback_messages=feedback.as_messages(),
            attempt_number=attempt,
            outcome=SelfEvaluateOutcome.REVISE,
        )


__all__ = [
    "EVALUATION_CRITERIA",
    "FakeSelfEvaluator",
    "GateAction",
    "GateConfig",
    "GateOutcome",
    "GradeTier",
    "SelfEvaluateFeedback",
    "SelfEvaluateGate",
    "SelfEvaluateOutcome",
    "SelfEvaluateVerdict",
    "SelfEvaluator",
]
