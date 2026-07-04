"""VT-605 (Loop Package 2, execution-plan §2) — the strict ManagerPlan/PlanStep models + the
NEW, versioned SpecialistReturn shape.

STRICT TYPES, NOT PROSE. A ``ManagerPlan`` is what the durable manager workflow (VT-606) persists
+ drives; every field is fail-closed validated at construction — an invalid plan never reaches the
store. Free-text fields (``situation`` / ``desired_outcome`` / prose ``acceptance_criteria``) are
redacted at PERSISTENCE time by ``plan_store`` (mirrors ``task_store``'s ``pii_redactor.redact``
discipline, CL-390) — this module only validates SHAPE, it does not redact.

--------------------------------------------------------------------------------------------------
AMENDMENT A1 (manager-loop-program.md, CC's binding amendment) — WHY ``PlanSpecialistReturn`` is
NOT named ``SpecialistReturn`` and does NOT replace ``orchestrator.agent.roster.SpecialistReturn``
--------------------------------------------------------------------------------------------------
The execution plan's §2 literally says "Replace the current return shape with: ...". A1 overrides
that for THIS row: "the SpecialistReturn type migration must not change legacy-path behavior during
shadow: an adapter keeps the tagged-union CampaignPlan -> collapse -> VT-594 owner-surfacing path
byte-compatible until enforce." The LIVE bridge (``agent/specialist_return.py``'s
``handle_specialist_return`` / ``observe_specialist_return``) constructs + consumes
``roster.SpecialistReturn`` (the 5-field dataclass: pushback/action_taken/outcome/proposed_outcome/
reason) TODAY, on live dispatch. Renaming or replacing that class here would either break the live
bridge or silently fork its behavior depending on import order — neither is acceptable pre-shadow.

So: ``PlanSpecialistReturn`` (below) is the NEW, richer shape from §2 — evidence-backed, effect-
intent-carrying, Manager-review-ready — added ALONGSIDE the legacy dataclass. NOTHING on the live
dispatch path constructs or consumes it yet. VT-606/607 build the adapter that bridges a
specialist's real output into this shape + (only then, gated) into the legacy tagged union for
byte-compatible shadow comparison (A1's "shadow must compare like-for-like").

WHY effect-class values reuse ``business_policy.PolicyActionClass`` — no new vocabulary: an
``EffectIntent.effect_class`` is a PROPOSAL that a Manager-review gate (VT-606) will route through
the EXISTING deterministic rails (``assert_within_policy`` / ``assert_or_gate_business_action``),
unchanged. Inventing a parallel effect-class enum here would either drift from the rails' own
vocabulary or require a translation layer for no reason — the plan step's ``allowed_effect_classes``
(below) reuses the SAME vocabulary for the identical reason.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.agents.business_policy import PolicyActionClass

_STRICT: ConfigDict = {"extra": "forbid"}

# The closed effect-class vocabulary a plan step / effect intent may declare — byte-identical to
# the deterministic rails' own taxonomy (customer_send / spend / commitment / config). A step or
# intent naming anything else fails closed (execution-plan §2: "Unknown ... effect class ... fails
# closed").
EFFECT_CLASSES: frozenset[str] = frozenset(c.value for c in PolicyActionClass)

# The Phase-1 roster — VT-604 pinned this to EXACTLY three. A specialist_dispatch step naming
# anything else fails closed (Literal below enforces it structurally, not just by convention).
_SPECIALISTS = ("onboarding_conductor", "integration_agent", "sales_recovery_agent")

StepKind = Literal[
    "specialist_dispatch",
    "advisory_tool",
    "clarification",
    "effect",
    "verification",
]

SpecialistName = Literal["onboarding_conductor", "integration_agent", "sales_recovery_agent"]


class PlanStep(BaseModel):
    """One step in a ``ManagerPlan`` (execution-plan §2, verbatim field set).

    Validation (fail-closed, all enforced HERE — not by convention downstream):
      - ``specialist`` is set if and only if ``kind == "specialist_dispatch"``.
      - An ``advisory_tool`` step declares NO effects (``allowed_effect_classes`` must be empty) —
        it analyses/prepares/drafts (VT-604 Package 1), it does not effect anything.
      - Every entry in ``allowed_effect_classes`` must be a known ``PolicyActionClass`` value.
      - ``step_seq`` >= 1 (the PARENT ``ManagerPlan`` enforces the full 1..N sequential/unique
        invariant across the whole step list — a single step cannot self-validate global order).
    """

    model_config = _STRICT

    step_seq: int = Field(..., ge=1)
    kind: StepKind
    specialist: SpecialistName | None = None
    situation: str = ""
    desired_outcome: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_effect_classes: list[str] = Field(default_factory=list)

    @field_validator("allowed_effect_classes")
    @classmethod
    def _known_effect_classes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - EFFECT_CLASSES)
        if unknown:
            raise ValueError(
                f"unknown effect class(es) {unknown!r}; must be one of {sorted(EFFECT_CLASSES)!r}"
            )
        return value

    @model_validator(mode="after")
    def _specialist_and_effect_rules(self) -> "PlanStep":
        if self.kind == "specialist_dispatch" and self.specialist is None:
            raise ValueError("a specialist_dispatch step requires a specialist")
        if self.kind != "specialist_dispatch" and self.specialist is not None:
            raise ValueError(
                f"specialist is only valid on a specialist_dispatch step (got kind={self.kind!r})"
            )
        if self.kind == "advisory_tool" and self.allowed_effect_classes:
            raise ValueError(
                "an advisory_tool step cannot declare effects (allowed_effect_classes must be empty)"
            )
        return self


class ManagerPlan(BaseModel):
    """The Manager's durable, executable plan for one objective-bearing task (execution-plan §2).

    ``plan_revision`` starts at 1; ``plan_store.revise_plan`` increments it and appends REPLACEMENT
    steps — it never edits a prior revision's steps in place (Package 2: "Revisions never edit
    completed history").
    """

    model_config = _STRICT

    schema_version: Literal["1"] = "1"
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(..., min_length=1, max_length=8)
    plan_revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _steps_sequential_unique_from_one(self) -> "ManagerPlan":
        seqs = [s.step_seq for s in self.steps]
        expected = list(range(1, len(seqs) + 1))
        if seqs != expected:
            raise ValueError(
                f"plan steps must be sequential, unique, and numbered from 1; "
                f"got step_seq={seqs!r}, expected {expected!r}"
            )
        return self


class EvidenceRef(BaseModel):
    """A by-value pointer to durable evidence backing a specialist's claimed outcome.

    Mirrors ``task_store``'s own ``evidence_kind`` / ``evidence_ref`` shape (migrations 152/165) so
    a ``PlanSpecialistReturn.evidence_refs`` entry maps 1:1 onto a ``manager_task_steps`` evidence
    pointer without translation. "Trust but verify": the Manager-review gate (VT-606) reads THESE,
    never the specialist's own prose claim of what it did.
    """

    model_config = _STRICT

    kind: Literal["campaign_plan", "agent_work_item", "pipeline_run", "pipeline_step"]
    ref: str = Field(..., min_length=1)


class EffectIntent(BaseModel):
    """A PROPOSED effect a specialist wants — NEVER a direct action (execution-plan §2: "Effect
    intents are proposals only and never execute directly").

    ``effect_class`` reuses ``business_policy.PolicyActionClass`` (see module docstring) so the
    Manager-review gate routes it through the EXISTING deterministic rail unchanged.
    """

    model_config = _STRICT

    effect_class: Literal["customer_send", "spend", "commitment", "config"]
    summary: str = Field(..., min_length=1)  # owner-facing framing, NEVER chain-of-thought (CL-390)
    magnitude_minor: int | None = Field(default=None, ge=0)  # paise; None for non-money effects

    @field_validator("effect_class")
    @classmethod
    def _known_effect_class(cls, value: str) -> str:
        if value not in EFFECT_CLASSES:
            raise ValueError(f"unknown effect_class {value!r}; must be one of {sorted(EFFECT_CLASSES)!r}")
        return value


class PlanSpecialistReturn(BaseModel):
    """VT-605 §2 — the NEW specialist->manager return shape (see the AMENDMENT A1 note above for
    why this is NOT ``orchestrator.agent.roster.SpecialistReturn`` and does not replace it yet).

    ``needs_owner_input`` requires ``owner_question``; ``blocked`` requires ``reason_code``
    (execution-plan §2, enforced here — fail-closed on construction, not by convention downstream).
    """

    model_config = _STRICT

    status: Literal["completed", "needs_owner_input", "blocked", "failed"]
    action_summary: str = ""
    outcome_summary: str = ""
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    effect_intents: list[EffectIntent] = Field(default_factory=list)
    owner_question: str | None = None
    proposed_outcome: str | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def _status_requires_fields(self) -> "PlanSpecialistReturn":
        if self.status == "needs_owner_input" and not self.owner_question:
            raise ValueError("status='needs_owner_input' requires owner_question")
        if self.status == "blocked" and not self.reason_code:
            raise ValueError("status='blocked' requires reason_code")
        return self


__all__ = [
    "EFFECT_CLASSES",
    "EffectIntent",
    "EvidenceRef",
    "ManagerPlan",
    "PlanSpecialistReturn",
    "PlanStep",
    "SpecialistName",
    "StepKind",
]
