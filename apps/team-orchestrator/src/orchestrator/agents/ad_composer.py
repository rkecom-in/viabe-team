"""VT-769 Ad Composer: complete campaign PROPOSALS, never advertising effects.

The output is a manually published, unpersisted artifact.  Publication/effect authority are class
constants rather than model-populated fields.  No ads SDK, transport, generic HTTP client, customer
record, credential or destination mint is reachable from this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from orchestrator.agent.tool_guardrail import assert_agent_tools_safe
from orchestrator.agents.artifact_contracts import ArtifactKind, ArtifactLineage, UnpersistedArtifact
from orchestrator.agents.content_branding import ContentFact, DraftCandidate, validate_quantitative_claims
from orchestrator.agents.sendless_guard import assert_file_sendless
from orchestrator.security.prompt_quarantine import FRAMING, fence, neutralize


AGENT_TOOLS: tuple[object, ...] = ()
assert_agent_tools_safe(AGENT_TOOLS, surface="agents.ad_composer")
assert_file_sendless(Path(__file__), surface="agents.ad_composer")

_TIER = "specialist"
_FENCE_RE = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_UTM_COMPONENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")
_CONTACT_KEYS = frozenset(
    {"phone", "phone_number", "email", "customer_id", "recipient", "customer_list", "audience_upload"}
)


class AdPlatform(StrEnum):
    META = "meta"
    GOOGLE = "google"


class DestinationRoute(StrEnum):
    FEASIBILITY_REPORT = "reports_feasibility"
    MARKET_INTELLIGENCE = "market_intelligence"


class ProposalValidationError(ValueError):
    """Raised when a campaign proposal is incomplete, unsafe or ungrounded."""


@dataclass(frozen=True, slots=True)
class ApprovedContentRef:
    artifact_id: str
    version: int
    locale: str
    text: str
    approved: bool

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or self.version < 1 or not self.locale.strip():
            raise ValueError("approved content reference needs id, positive version and locale")
        if not self.approved:
            raise ValueError("Ad Composer accepts only owner/Manager-approved content artifacts")


@dataclass(frozen=True, slots=True)
class TrackedDestinationRequest:
    """Typed request only.  It deliberately has no resolved URL or minting method."""

    route: DestinationRoute
    utm_source: str
    utm_medium: str
    utm_campaign: str
    utm_content: str

    def __post_init__(self) -> None:
        for name in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
            value = str(getattr(self, name))
            if _UTM_COMPONENT.fullmatch(value) is None:
                raise ValueError(f"{name} must be lowercase kebab-case")
            if _EMAIL.search(value) or _PHONE.search(value):
                raise ValueError(f"{name} must not contain PII")

    def as_dict(self) -> dict[str, str]:
        return {
            "route": self.route.value,
            "utm_source": self.utm_source,
            "utm_medium": self.utm_medium,
            "utm_campaign": self.utm_campaign,
            "utm_content": self.utm_content,
            "resolution": "unresolved",
        }


@dataclass(frozen=True, slots=True)
class CampaignAssignment:
    platform: AdPlatform
    objective: str
    audience_hypothesis: str
    geography: str
    locale: str
    owner_budget_min_paise: int
    owner_budget_max_paise: int
    campaign_start: datetime
    campaign_end: datetime
    destination: TrackedDestinationRequest
    approved_content: tuple[ApprovedContentRef, ...]
    facts: tuple[ContentFact, ...]
    aggregate_context: Mapping[str, object] | None = None
    lineage: ArtifactLineage = ArtifactLineage()

    def __post_init__(self) -> None:
        if not self.objective.strip() or not self.audience_hypothesis.strip() or not self.geography.strip():
            raise ValueError("campaign objective, audience hypothesis and geography are required")
        if self.locale not in {"en", "hi", "hinglish"}:
            raise ValueError("campaign locale must be en, hi or hinglish")
        if self.owner_budget_min_paise <= 0 or self.owner_budget_max_paise < self.owner_budget_min_paise:
            raise ValueError("owner budget range is invalid")
        if self.campaign_start.tzinfo is None or self.campaign_end.tzinfo is None:
            raise ValueError("campaign dates must be timezone-aware")
        if self.campaign_end <= self.campaign_start:
            raise ValueError("campaign end must be after start")
        if not isinstance(self.destination, TrackedDestinationRequest):
            raise ValueError("a typed tracked-destination request is required")
        if not self.approved_content:
            raise ValueError("at least one approved creative artifact is required")
        keys = {str(key).lower() for key in (self.aggregate_context or {})}
        overlap = keys & _CONTACT_KEYS
        if overlap:
            raise ValueError(f"customer/contact-level ad input is forbidden: {sorted(overlap)}")


@dataclass(frozen=True, slots=True)
class CampaignCandidate:
    primary_objective: str
    audience_spec: str
    structure: str
    budget_paise: int
    daily_budget_paise: int
    creative_refs: tuple[str, ...]
    success_metric: str
    event_source: str
    kill_spend_paise: int
    kill_min_events: int
    kill_action: str
    measurement_limits: tuple[str, ...]
    quantitative_copy: tuple[str, ...]
    fact_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CampaignProposal:
    """Immutable proposal authority boundary; instances cannot opt into effects/publication."""

    PUBLICATION_MODE: ClassVar[str] = "manual_owner_only"
    AUTHORIZES_EFFECTS: ClassVar[bool] = False

    artifact: UnpersistedArtifact
    destination_request: TrackedDestinationRequest

    def as_proposal(self) -> dict[str, object]:
        if type(self).PUBLICATION_MODE != "manual_owner_only" or type(self).AUTHORIZES_EFFECTS:
            raise RuntimeError("Ad Composer authority constants were widened; refusing proposal")
        result = self.artifact.as_proposal()
        result["publication_mode"] = type(self).PUBLICATION_MODE
        result["effect_authorized"] = type(self).AUTHORIZES_EFFECTS
        return result


TextCall = Callable[..., str]


def build_prompt(assignment: CampaignAssignment, *, at: datetime) -> tuple[str, str]:
    facts = [
        {
            "fact_id": fact.fact_id,
            "value": fence(fact.value, source="ad_fact.value", max_len=120),
            "unit": fence(fact.unit, source="ad_fact.unit", max_len=40),
            "period": fence(fact.period, source="ad_fact.period", max_len=120),
            "source_ref": fence(fact.source_ref, source="ad_fact.source_ref", max_len=300),
        }
        for fact in assignment.facts
        if fact.usable_at(at)
    ]
    creatives = [
        {
            "artifact_id": ref.artifact_id,
            "version": ref.version,
            "locale": ref.locale,
            "text": fence(ref.text, source="approved_content.text", max_len=3000),
        }
        for ref in assignment.approved_content
    ]
    aggregate = {
        str(key): fence(str(value), source=f"aggregate_campaign.{key}", max_len=500)
        for key, value in (assignment.aggregate_context or {}).items()
    }
    user = json.dumps(
        {
            "platform": assignment.platform.value,
            "objective": fence(assignment.objective, source="owner.ad_objective", max_len=1000),
            "audience_hypothesis": fence(
                assignment.audience_hypothesis, source="owner.audience_hypothesis", max_len=1000
            ),
            "geography": fence(assignment.geography, source="owner.target_geography", max_len=200),
            "locale": assignment.locale,
            "owner_budget_min_paise": assignment.owner_budget_min_paise,
            "owner_budget_max_paise": assignment.owner_budget_max_paise,
            "campaign_start": assignment.campaign_start.isoformat(),
            "campaign_end": assignment.campaign_end.isoformat(),
            "destination_request": assignment.destination.as_dict(),
            "approved_content": creatives,
            "facts": facts,
            "aggregate_context": aggregate,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    system = (
        f"{FRAMING}\n"
        "Compose one complete draft-only Meta/Google campaign proposal. Return strict JSON with "
        "primary_objective, audience_spec, structure, budget_paise, daily_budget_paise, "
        "creative_refs, success_metric, event_source, kill_spend_paise, kill_min_events, "
        "kill_action, measurement_limits, quantitative_copy and fact_refs. Budget must remain "
        "inside the owner range. Every quantitative performance claim must bind to a supplied fact. "
        "Never invent a destination URL, measured result, audience list or publication state."
    )
    return system, user


def _parse_candidate(raw: str) -> CampaignCandidate:
    text = raw.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group("body").strip()
    parsed = json.loads(text)
    required = {
        "primary_objective",
        "audience_spec",
        "structure",
        "budget_paise",
        "daily_budget_paise",
        "creative_refs",
        "success_metric",
        "event_source",
        "kill_spend_paise",
        "kill_min_events",
        "kill_action",
        "measurement_limits",
        "quantitative_copy",
        "fact_refs",
    }
    if not isinstance(parsed, dict) or set(parsed) != required:
        raise ProposalValidationError(f"campaign fields must be exactly {sorted(required)}")
    for key in ("creative_refs", "measurement_limits", "quantitative_copy", "fact_refs"):
        if not isinstance(parsed[key], list):
            raise ProposalValidationError(f"{key} must be an array")
    return CampaignCandidate(
        primary_objective=neutralize(str(parsed["primary_objective"])).strip(),
        audience_spec=neutralize(str(parsed["audience_spec"])).strip(),
        structure=neutralize(str(parsed["structure"])).strip(),
        budget_paise=int(parsed["budget_paise"]),
        daily_budget_paise=int(parsed["daily_budget_paise"]),
        creative_refs=tuple(str(value).strip() for value in parsed["creative_refs"]),
        success_metric=neutralize(str(parsed["success_metric"])).strip(),
        event_source=neutralize(str(parsed["event_source"])).strip(),
        kill_spend_paise=int(parsed["kill_spend_paise"]),
        kill_min_events=int(parsed["kill_min_events"]),
        kill_action=neutralize(str(parsed["kill_action"])).strip(),
        measurement_limits=tuple(neutralize(str(value)).strip() for value in parsed["measurement_limits"]),
        quantitative_copy=tuple(neutralize(str(value)).strip() for value in parsed["quantitative_copy"]),
        fact_refs=tuple(str(value).strip() for value in parsed["fact_refs"]),
    )


def validate_campaign_candidate(
    candidate: CampaignCandidate,
    assignment: CampaignAssignment,
    *,
    at: datetime,
) -> None:
    required_text = (
        candidate.primary_objective,
        candidate.audience_spec,
        candidate.structure,
        candidate.success_metric,
        candidate.event_source,
        candidate.kill_action,
    )
    if not all(required_text):
        raise ProposalValidationError("proposal has an empty required field")
    if not assignment.owner_budget_min_paise <= candidate.budget_paise <= assignment.owner_budget_max_paise:
        raise ProposalValidationError("recommended budget is outside the owner-approved range")
    if candidate.daily_budget_paise <= 0 or candidate.daily_budget_paise > candidate.budget_paise:
        raise ProposalValidationError("daily budget is invalid")
    if candidate.kill_spend_paise <= 0 or candidate.kill_spend_paise > candidate.budget_paise:
        raise ProposalValidationError("kill spend must be positive and within recommended budget")
    if candidate.kill_min_events < 0 or not candidate.success_metric or not candidate.event_source:
        raise ProposalValidationError("success metric/event source/kill threshold is incomplete")
    if not candidate.measurement_limits:
        raise ProposalValidationError("proposal must state attribution/measurement limits")
    expected_refs = {f"{ref.artifact_id}:v{ref.version}" for ref in assignment.approved_content}
    if not candidate.creative_refs or set(candidate.creative_refs) - expected_refs:
        raise ProposalValidationError("proposal references missing or unapproved creative artifacts")
    # The runtime fact validator gates performance/market claims in free-form quantitative copy.
    binding_candidate = DraftCandidate(
        headline=candidate.primary_objective,
        blocks=(
            candidate.audience_spec,
            candidate.structure,
            candidate.success_metric,
            candidate.event_source,
            *candidate.measurement_limits,
            *candidate.quantitative_copy,
        ),
        call_to_action=candidate.kill_action,
        fact_refs=candidate.fact_refs,
    )
    validate_quantitative_claims(binding_candidate, assignment.facts, at=at)


def compose_campaign_proposal(
    assignment: CampaignAssignment,
    *,
    text_call: TextCall | None = None,
    now: datetime | None = None,
) -> CampaignProposal:
    at = now or datetime.now(UTC)
    system, user = build_prompt(assignment, at=at)
    if text_call is None:
        from orchestrator.llm.structured import structured_text_call

        text_call = structured_text_call
    raw = text_call(
        _TIER,
        system=system,
        user=user,
        max_tokens=2200,
        agent="ad_composer",
        call_site="ad_composer_propose",
        timeout_s=45.0,
    )
    candidate = _parse_candidate(raw)
    validate_campaign_candidate(candidate, assignment, at=at)
    digest = hashlib.sha256(
        json.dumps(
            {
                "platform": assignment.platform.value,
                "objective": candidate.primary_objective,
                "budget": candidate.budget_paise,
                "creatives": candidate.creative_refs,
                "destination": assignment.destination.as_dict(),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    payload: dict[str, object] = {
        "platform": assignment.platform.value,
        "primary_objective": candidate.primary_objective,
        "audience_spec": candidate.audience_spec,
        "structure": candidate.structure,
        "budget_paise": candidate.budget_paise,
        "daily_budget_paise": candidate.daily_budget_paise,
        "creative_refs": list(candidate.creative_refs),
        "success_metric": candidate.success_metric,
        "event_source": candidate.event_source,
        "kill_criterion": {
            "spend_paise": candidate.kill_spend_paise,
            "minimum_attributed_events": candidate.kill_min_events,
            "action": candidate.kill_action,
        },
        "measurement_limits": list(candidate.measurement_limits),
        "fact_refs": list(candidate.fact_refs),
        "destination_request": assignment.destination.as_dict(),
        "destination_url": None,
    }
    artifact = UnpersistedArtifact(
        artifact_id=f"campaign-{digest}",
        kind=ArtifactKind.CAMPAIGN_PROPOSAL,
        version=(assignment.lineage.parent_version or 0) + 1,
        created_at=at,
        payload=payload,
        lineage=assignment.lineage,
    )
    proposal = CampaignProposal(artifact=artifact, destination_request=assignment.destination)
    # Assert the class boundary at the serving seam; monkeypatching either constant makes it refuse.
    proposal.as_proposal()
    return proposal


__all__ = [
    "AGENT_TOOLS",
    "AdPlatform",
    "ApprovedContentRef",
    "CampaignAssignment",
    "CampaignCandidate",
    "CampaignProposal",
    "DestinationRoute",
    "ProposalValidationError",
    "TrackedDestinationRequest",
    "build_prompt",
    "compose_campaign_proposal",
    "validate_campaign_candidate",
]
