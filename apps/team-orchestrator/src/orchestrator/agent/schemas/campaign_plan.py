"""CampaignPlan v1.0 — agent structured-output contract (VT-37).

Supersedes the v0.1 7-field plumbing model at
``orchestrator.types.campaign_plan`` (CL-177). The v0.1 model is left
intact on `main` — VT-3.4 plumbing code still imports it; migration is
a separate follow-up subtask (see PR body).

Status enum split (load-bearing)
--------------------------------
``CampaignPlan.status`` carries only the THREE agent-terminal states:

  - ``proposed``         — actionable campaign produced; all proposed fields present
  - ``out_of_scope``     — input outside Sales Recovery domain; refusal reason carried
  - ``insufficient_data``— in-scope but not enough context; missing-data list carried

Lifecycle states ``approved`` / ``rejected`` / ``sent`` / ``failed`` are
NOT on this contract. They belong to a downstream lifecycle field owned
by campaigns-schema / owner-surface (separate subtask). VT-37 does not
"drop" those states — they live on a different field.

Pillar 7 (honesty)
------------------
Structural enforcement:

- ``expected_arrr`` uses ``low_paise`` + ``high_paise`` + a ``confidence``
  enum + a textual ``basis`` — point estimates are FORBIDDEN.
- ``evidence_refs`` non-empty on the ``proposed`` variant; every
  ``[E\\d+]``-style marker in the prose fields (selection_reason, basis)
  must resolve to a declared ``claim_id``, AND every declared
  ``claim_id`` must be cited at least once. Unbacked prose claims and
  uncited evidence both fail validation.

All currency: integer paise. Never float.

Out of VT-37 (follow-ups, not built here)
----------------------------------------
- ``approved_templates.yaml`` registry + ``template_id`` registry validator
  (owner-surface / Meta-template-approval scope)
- ``serializer.py`` (``to_orchestrator_dict``, ``from_agent_output``)
  — agent↔orchestrator wire format
- ``attribution_close_at == send_at + 7d`` validator
  (``send_at`` does not exist at agent-output time; that validator
  lives wherever ``send_at`` is set)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

# ----------------------------------------------------------------------------
# Enums + atomic shapes
# ----------------------------------------------------------------------------


class CampaignStatus(str, Enum):
    """The three agent-terminal states (lifecycle states are downstream)."""

    PROPOSED = "proposed"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_DATA = "insufficient_data"


class SelfEvaluateStatus(str, Enum):
    """Populated by the VT-4.5 self-evaluate gate, NOT by the agent.

    Default is ``not_yet_evaluated`` so a freshly-produced draft is
    distinguishable from one that has been through the gate. VT-37 does
    not enforce this field — the gate that forces the evaluation lives
    in VT-4.5.
    """

    NOT_YET_EVALUATED = "not_yet_evaluated"
    PASSED = "passed"
    FAILED_AFTER_REVISIONS = "failed_after_revisions"


class EvidenceSourceKind(str, Enum):
    """Typed source kind for an evidence ref. No free strings."""

    TOOL_CALL = "tool_call"
    L4_SKILL_CORPUS = "l4_skill_corpus"
    L2_EPISODIC_MEMORY = "l2_episodic_memory"


class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Language(str, Enum):
    EN = "en"
    HI = "hi"


class EscalationSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SuggestedSpecialist(str, Enum):
    REPUTATION = "reputation"
    MARKETING = "marketing"
    OPERATIONS = "operations"


# ----------------------------------------------------------------------------
# Sub-models (used by the proposed variant)
# ----------------------------------------------------------------------------


_STRICT: ConfigDict = {"extra": "forbid"}

_MARKER_RE = re.compile(r"\[(E\d+)\]")


class CampaignWindow(BaseModel):
    """Time window in which the campaign is intended to run.

    Validator: ``end > start``; the window must not be in the past
    (start strictly in the future). Tolerance: start may equal "now" at
    the moment of validation, but a past start is rejected — agents
    that emit a window with a backdated start are treated as malformed.
    """

    model_config = _STRICT

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _window_validity(self) -> "CampaignWindow":
        if self.end <= self.start:
            raise ValueError(
                f"campaign_window.end ({self.end.isoformat()}) must be > "
                f"start ({self.start.isoformat()})"
            )
        now = datetime.now(UTC)
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("campaign_window timestamps must be timezone-aware")
        if self.start < now:
            raise ValueError(
                f"campaign_window.start ({self.start.isoformat()}) is in "
                f"the past relative to now ({now.isoformat()})"
            )
        return self


class TargetCohort(BaseModel):
    """The explicit customer cohort the campaign targets.

    ``customer_ids`` is the LITERAL list — not a query, not a filter
    expression. ``cohort_size`` MUST equal ``len(customer_ids)``; the
    redundancy is intentional (the agent commits to a size that matches
    the list it actually returned).
    """

    model_config = _STRICT

    customer_ids: list[UUID]
    cohort_label: str = Field(..., min_length=1)
    cohort_size: int = Field(..., ge=1)
    selection_reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _size_matches_list(self) -> "TargetCohort":
        if self.cohort_size != len(self.customer_ids):
            raise ValueError(
                f"cohort_size ({self.cohort_size}) must equal "
                f"len(customer_ids) ({len(self.customer_ids)})"
            )
        return self


class ExpectedARRR(BaseModel):
    """Recoverable ARR range, in paise (integer).

    Point estimates are forbidden — the range itself is the
    Pillar-7-honest output. ``low_paise <= high_paise``, both ≥ 0.
    """

    model_config = _STRICT

    low_paise: int = Field(..., ge=0)
    high_paise: int = Field(..., ge=0)
    confidence: ConfidenceLevel
    basis: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> "ExpectedARRR":
        if self.low_paise > self.high_paise:
            raise ValueError(
                f"expected_arrr.low_paise ({self.low_paise}) must be "
                f"<= high_paise ({self.high_paise})"
            )
        return self


class EvidenceRef(BaseModel):
    """A reference backing a prose claim.

    The ``claim_id`` is the token referenced in the prose with a
    ``[E1]`` / ``[E2]`` / ``[E\\d+]`` marker. The cross-field validator
    on the proposed variant enforces marker↔claim_id consistency in
    both directions.
    """

    model_config = _STRICT

    claim_id: str = Field(..., pattern=r"^E\d+$")
    source_kind: EvidenceSourceKind
    source_id: str = Field(..., min_length=1)
    note: str | None = None


class MessagePlan(BaseModel):
    """The message content the campaign will send.

    ``template_id`` references an approved Meta WhatsApp template. The
    registry validator that confirms ``template_id`` is in
    ``approved_templates.yaml`` is OUT of VT-37 scope (owner-surface /
    Meta-template-approval subtask). For now the field is a free string
    at this layer; downstream validation rejects unknowns.
    """

    model_config = _STRICT

    template_id: str = Field(..., min_length=1)
    template_params: dict[str, str]
    language: Language
    personalization: str = Field(..., min_length=1)


class EscalationCondition(BaseModel):
    """A structured trigger that routes to Fazal before send."""

    model_config = _STRICT

    trigger: str = Field(..., min_length=1)
    severity: EscalationSeverity = EscalationSeverity.MEDIUM
    threshold: int | None = None  # optional numeric threshold (e.g. cohort_size > 500)


class MissingDataItem(BaseModel):
    """A single missing-context entry for the insufficient_data variant."""

    model_config = _STRICT

    category: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    suggested_remediation: str = Field(..., min_length=1)


# ----------------------------------------------------------------------------
# Variants
# ----------------------------------------------------------------------------


class _CampaignPlanBase(BaseModel):
    """Common identity + provenance fields on every variant."""

    model_config = _STRICT

    version: Literal["1.0"] = "1.0"
    tenant_id: UUID
    run_id: UUID
    generated_at: datetime
    self_evaluate_status: SelfEvaluateStatus = SelfEvaluateStatus.NOT_YET_EVALUATED

    @field_validator("generated_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return v


class CampaignPlanProposed(_CampaignPlanBase):
    """The actionable-campaign variant.

    All campaign fields are required. Evidence-marker consistency is
    enforced across the prose-bearing fields (selection_reason, basis).
    """

    status: Literal[CampaignStatus.PROPOSED] = CampaignStatus.PROPOSED

    campaign_window: CampaignWindow
    target_cohort: TargetCohort
    expected_arrr: ExpectedARRR
    evidence_refs: list[EvidenceRef] = Field(..., min_length=1)
    message_plan: MessagePlan
    exclusion_list: list[UUID] = Field(default_factory=list)
    exclusion_reasons: dict[UUID, str] = Field(default_factory=dict)
    escalation_conditions: list[EscalationCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_marker_consistency(self) -> "CampaignPlanProposed":
        prose_blocks = [
            self.target_cohort.selection_reason,
            self.expected_arrr.basis,
        ]
        cited: set[str] = set()
        for prose in prose_blocks:
            for match in _MARKER_RE.finditer(prose):
                cited.add(match.group(1))
        declared = {ref.claim_id for ref in self.evidence_refs}

        unbacked = cited - declared
        uncited = declared - cited
        if unbacked:
            raise ValueError(
                f"prose claim markers without backing evidence_refs: "
                f"{sorted(unbacked)}"
            )
        if uncited:
            raise ValueError(
                f"evidence_refs not cited by any prose marker: "
                f"{sorted(uncited)}"
            )
        return self

    @model_validator(mode="after")
    def _exclusion_reasons_keyset(self) -> "CampaignPlanProposed":
        """Every customer in exclusion_list must have a reason; no orphan
        reasons keyed to customers not in the exclusion_list."""
        list_set = set(self.exclusion_list)
        reasons_set = set(self.exclusion_reasons.keys())
        missing_reasons = list_set - reasons_set
        orphan_reasons = reasons_set - list_set
        if missing_reasons:
            raise ValueError(
                f"exclusion_list entries missing reasons: {sorted(map(str, missing_reasons))}"
            )
        if orphan_reasons:
            raise ValueError(
                f"exclusion_reasons for customers not in exclusion_list: "
                f"{sorted(map(str, orphan_reasons))}"
            )
        return self


class CampaignPlanOutOfScope(_CampaignPlanBase):
    """The refusal variant — input outside Sales Recovery domain."""

    status: Literal[CampaignStatus.OUT_OF_SCOPE] = CampaignStatus.OUT_OF_SCOPE

    out_of_scope_reason: str = Field(..., min_length=1, max_length=500)
    suggested_specialist: SuggestedSpecialist | None = None


class CampaignPlanInsufficientData(_CampaignPlanBase):
    """The defer variant — in-scope but not enough context."""

    status: Literal[CampaignStatus.INSUFFICIENT_DATA] = CampaignStatus.INSUFFICIENT_DATA

    missing_data: list[MissingDataItem] = Field(..., min_length=1)


# ----------------------------------------------------------------------------
# Top-level discriminated union + parser
# ----------------------------------------------------------------------------


CampaignPlan: TypeAlias = Annotated[
    CampaignPlanProposed | CampaignPlanOutOfScope | CampaignPlanInsufficientData,
    Field(discriminator="status"),
]


# TypeAdapter is the pydantic-v2 way to validate against a tagged union
# without wrapping it in another BaseModel. Module-level so callers
# don't pay the build cost per parse.
_CAMPAIGN_PLAN_ADAPTER: TypeAdapter[CampaignPlan] = TypeAdapter(CampaignPlan)


def parse_campaign_plan(data: object) -> CampaignPlan:
    """Validate raw input (dict / JSON-like) into a typed ``CampaignPlan``.

    The discriminator on ``status`` selects the variant; pydantic
    rejects payloads where the variant's required fields are absent
    OR where forbidden fields (e.g. ``campaign_window`` on an
    ``out_of_scope`` payload) are present.
    """
    return _CAMPAIGN_PLAN_ADAPTER.validate_python(data)


__all__ = [
    "CampaignPlan",
    "CampaignPlanInsufficientData",
    "CampaignPlanOutOfScope",
    "CampaignPlanProposed",
    "CampaignStatus",
    "CampaignWindow",
    "ConfidenceLevel",
    "EscalationCondition",
    "EscalationSeverity",
    "EvidenceRef",
    "EvidenceSourceKind",
    "ExpectedARRR",
    "Language",
    "MessagePlan",
    "MissingDataItem",
    "SelfEvaluateStatus",
    "SuggestedSpecialist",
    "TargetCohort",
    "parse_campaign_plan",
]
