"""VT-768 Content/Branding specialist: grounded, quarantined, structurally sendless drafts.

This module may call the governed LLM seam to COMPOSE text.  It never publishes, sends, persists,
or addresses a customer.  Every untrusted input is fenced before prompt use and every quantitative
claim is checked at runtime against the supplied fact bundle before a draft can leave the module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from orchestrator.agent.tool_guardrail import assert_agent_tools_safe
from orchestrator.agents.artifact_contracts import ArtifactKind, ArtifactLineage, UnpersistedArtifact
from orchestrator.agents.sendless_guard import assert_file_sendless
from orchestrator.security.prompt_quarantine import FRAMING, fence, neutralize


AGENT_TOOLS: tuple[object, ...] = ()
assert_agent_tools_safe(AGENT_TOOLS, surface="agents.content_branding")
assert_file_sendless(Path(__file__), surface="agents.content_branding")

_NUMERIC = re.compile(r"(?<![A-Za-z0-9_])(?:₹\s*)?-?\d[\d,]*(?:\.\d+)?(?:\s*%)?")
_FENCE_RE = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_CONTACT_KEYS = frozenset({"phone", "phone_number", "email", "customer_id", "recipient"})
_TIER = "specialist"


class ContentLocale(StrEnum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class ContentArtifactType(StrEnum):
    SOCIAL_POST = "social_post"
    LANDING_COPY = "landing_copy"
    LAUNCH_COPY = "launch_copy"
    REPORT_CREATIVE_BRIEF = "report_creative_brief"
    WHATSAPP_STATUS = "whatsapp_status"


class FactBindingError(ValueError):
    """Raised when generated quantitative copy is not bound to usable supplied facts."""


@dataclass(frozen=True, slots=True)
class ContentFact:
    fact_id: str
    value: str
    unit: str
    period: str
    source_ref: str
    measured_at: datetime
    valid_through: datetime | None = None
    connected: bool = True

    def __post_init__(self) -> None:
        for name in ("fact_id", "value", "unit", "period", "source_ref"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"ContentFact.{name} is required")
        if self.measured_at.tzinfo is None:
            raise ValueError("ContentFact.measured_at must be timezone-aware")
        if self.valid_through is not None and self.valid_through.tzinfo is None:
            raise ValueError("ContentFact.valid_through must be timezone-aware")

    def usable_at(self, at: datetime) -> bool:
        return self.connected and (self.valid_through is None or self.valid_through >= at)

    @property
    def rendered_value(self) -> str:
        return f"{self.value}{self.unit}" if self.unit == "%" else f"{self.value} {self.unit}".strip()


@dataclass(frozen=True, slots=True)
class BrandVoiceProfile:
    positioning: str
    tone: str
    permitted_product_names: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()
    vocabulary: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContentAssignment:
    objective: str
    audience: str
    channel: str
    artifact_type: ContentArtifactType
    locale: ContentLocale
    offer_copy: str
    voice: BrandVoiceProfile | None
    facts: tuple[ContentFact, ...]
    lineage: ArtifactLineage = ArtifactLineage()
    aggregate_context: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for name in ("objective", "audience", "channel"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"ContentAssignment.{name} is required")
        keys = {str(key).lower() for key in (self.aggregate_context or {})}
        overlap = keys & _CONTACT_KEYS
        if overlap:
            raise ValueError(f"customer/contact-level input is forbidden: {sorted(overlap)}")


@dataclass(frozen=True, slots=True)
class DraftCandidate:
    headline: str
    blocks: tuple[str, ...]
    call_to_action: str
    fact_refs: tuple[str, ...]
    warnings: tuple[str, ...] = ()


TextCall = Callable[..., str]


def _numeric_claims(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).replace(" ", "") for match in _NUMERIC.finditer(text))


def validate_quantitative_claims(
    candidate: DraftCandidate,
    facts: Sequence[ContentFact],
    *,
    at: datetime,
) -> tuple[str, ...]:
    """Live fabricated-number gate; eval helpers are deliberately not imported."""

    by_id = {fact.fact_id: fact for fact in facts}
    if len(by_id) != len(facts):
        raise FactBindingError("fact ids must be unique")
    unknown = set(candidate.fact_refs) - set(by_id)
    if unknown:
        raise FactBindingError(f"candidate references unknown facts: {sorted(unknown)}")
    unusable = {ref for ref in candidate.fact_refs if not by_id[ref].usable_at(at)}
    if unusable:
        raise FactBindingError(f"candidate references stale/disconnected facts: {sorted(unusable)}")

    copy = "\n".join((candidate.headline, *candidate.blocks, candidate.call_to_action))
    claims = set(_numeric_claims(copy))
    allowed: set[str] = set()
    for ref in candidate.fact_refs:
        fact = by_id[ref]
        allowed.update(_numeric_claims(fact.rendered_value))
    unsupported = claims - allowed
    if unsupported:
        raise FactBindingError(
            f"quantitative claim(s) are not reproducible from referenced supplied facts: "
            f"{sorted(unsupported)}"
        )
    if claims and not candidate.fact_refs:
        raise FactBindingError("quantitative copy requires at least one supplied fact reference")
    return tuple(sorted(claims))


def build_prompt(assignment: ContentAssignment, *, at: datetime) -> tuple[str, str]:
    """Render the system/user prompt with every owner/external field fenced."""

    voice = assignment.voice
    voice_payload = {
        "positioning": voice.positioning if voice else "neutral",
        "tone": voice.tone if voice else "neutral",
        "permitted_product_names": list(voice.permitted_product_names if voice else ()),
        "forbidden_phrases": list(voice.forbidden_phrases if voice else ()),
        "vocabulary": list(voice.vocabulary if voice else ()),
        "examples": list(voice.examples if voice else ()),
    }
    fenced_voice = {
        key: (
            [fence(str(item), source=f"brand_voice.{key}", max_len=500) for item in value]
            if isinstance(value, list)
            else fence(str(value), source=f"brand_voice.{key}", max_len=1000)
        )
        for key, value in voice_payload.items()
    }
    usable_facts = [fact for fact in assignment.facts if fact.usable_at(at)]
    facts_payload = [
        {
            "fact_id": fact.fact_id,
            "value": fence(fact.value, source="content_fact.value", max_len=120),
            "unit": fence(fact.unit, source="content_fact.unit", max_len=40),
            "period": fence(fact.period, source="content_fact.period", max_len=120),
            "source_ref": fence(fact.source_ref, source="content_fact.source_ref", max_len=300),
            "measured_at": fact.measured_at.isoformat(),
        }
        for fact in usable_facts
    ]
    aggregate = {
        str(key): fence(str(value), source=f"aggregate.{key}", max_len=500)
        for key, value in (assignment.aggregate_context or {}).items()
    }
    user = json.dumps(
        {
            "objective": fence(assignment.objective, source="manager.objective", max_len=1000),
            "audience": fence(assignment.audience, source="manager.audience", max_len=500),
            "channel": fence(assignment.channel, source="manager.channel", max_len=100),
            "offer_copy": fence(assignment.offer_copy, source="owner.offer_copy", max_len=1000),
            "artifact_type": assignment.artifact_type.value,
            "locale": assignment.locale.value,
            "brand_voice": fenced_voice,
            "facts": facts_payload,
            "aggregate_context": aggregate,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    system = (
        f"{FRAMING}\n"
        "Compose one owner-reviewable marketing draft. Return strict JSON with headline, blocks "
        "(array), call_to_action, fact_refs (array), warnings (array). Use only supplied facts for "
        "numbers or performance claims. Missing facts are omitted, never rendered as zero. Preserve "
        "the requested en/hi/hinglish register. Do not address a customer and do not claim storage, "
        "publication, sending, superiority, trust, or measurement unless explicitly supplied."
    )
    return system, user


def _parse_candidate(raw: str) -> DraftCandidate:
    text = raw.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group("body").strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("content composer output must be a JSON object")
    required = {"headline", "blocks", "call_to_action", "fact_refs", "warnings"}
    if set(parsed) != required:
        raise ValueError(f"content composer fields must be exactly {sorted(required)}")
    if not isinstance(parsed["blocks"], list) or not parsed["blocks"]:
        raise ValueError("content composer blocks must be a non-empty array")
    if not isinstance(parsed["fact_refs"], list) or not isinstance(parsed["warnings"], list):
        raise ValueError("content composer fact_refs/warnings must be arrays")
    candidate = DraftCandidate(
        headline=neutralize(str(parsed["headline"])).strip(),
        blocks=tuple(neutralize(str(value)).strip() for value in parsed["blocks"]),
        call_to_action=neutralize(str(parsed["call_to_action"])).strip(),
        fact_refs=tuple(str(value).strip() for value in parsed["fact_refs"]),
        warnings=tuple(neutralize(str(value)).strip() for value in parsed["warnings"]),
    )
    if not candidate.headline or not all(candidate.blocks) or not candidate.call_to_action:
        raise ValueError("content composer returned an empty required copy field")
    return candidate


def compose_content_artifact(
    assignment: ContentAssignment,
    *,
    text_call: TextCall | None = None,
    now: datetime | None = None,
) -> UnpersistedArtifact:
    at = now or datetime.now(UTC)
    system, user = build_prompt(assignment, at=at)
    if text_call is None:
        from orchestrator.llm.structured import structured_text_call

        text_call = structured_text_call
    raw = text_call(
        _TIER,
        system=system,
        user=user,
        max_tokens=1800,
        agent="content_branding",
        call_site="content_branding_compose",
        timeout_s=45.0,
    )
    candidate = _parse_candidate(raw)
    numeric_claims = validate_quantitative_claims(candidate, assignment.facts, at=at)
    digest = hashlib.sha256(
        json.dumps(
            {
                "type": assignment.artifact_type.value,
                "locale": assignment.locale.value,
                "headline": candidate.headline,
                "blocks": candidate.blocks,
                "cta": candidate.call_to_action,
                "facts": candidate.fact_refs,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    payload: dict[str, object] = {
        "artifact_type": assignment.artifact_type.value,
        "locale": assignment.locale.value,
        "register": assignment.locale.value,
        "headline": candidate.headline,
        "blocks": list(candidate.blocks),
        "call_to_action": candidate.call_to_action,
        "fact_refs": list(candidate.fact_refs),
        "numeric_claims": list(numeric_claims),
        "warnings": [
            *(candidate.warnings),
            *(() if assignment.voice else ("neutral_voice_profile_used",)),
        ],
    }
    return UnpersistedArtifact(
        artifact_id=f"content-{digest}",
        kind=ArtifactKind.CONTENT_DRAFT,
        version=(assignment.lineage.parent_version or 0) + 1,
        created_at=at,
        payload=payload,
        lineage=assignment.lineage,
    )


__all__ = [
    "AGENT_TOOLS",
    "BrandVoiceProfile",
    "ContentArtifactType",
    "ContentAssignment",
    "ContentFact",
    "ContentLocale",
    "DraftCandidate",
    "FactBindingError",
    "build_prompt",
    "compose_content_artifact",
    "validate_quantitative_claims",
]
