"""Sendless acquisition research for public Indian F&B launch signals.

The module normalises evidence already acquired by a read-only source adapter. It performs no web
request and holds no transport: search/acquisition is injected upstream, while this core validates,
scores, deduplicates and returns inert prospect artifacts. A public phone can be observed by an
adapter but is deliberately erased at this boundary; it is never an outreach consent basis.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

AGENT_NAME = "acquisition_prospector"
AGENT_TOOLS: tuple[Any, ...] = ()
assert_agent_tools_safe(AGENT_TOOLS, surface="agents.acquisition_prospector")

_SPACE_RE = re.compile(r"\s+")
_STAGE_POINTS = {
    "live_waitlist": 35,
    "pilot": 35,
    "newly_incorporated": 32,
    "preopening_hiring": 30,
    "opening_soon": 28,
    "launch_signal_revalidate": 12,
    "launched_revalidate": 8,
    "unknown": 0,
}


class EvidenceClass(str, Enum):
    GOVERNMENT = "government"
    OPERATOR_OWNED = "operator_owned"
    FOUNDER_OWNED = "founder_owned"
    REPUTABLE_SECONDARY = "reputable_secondary"
    PUBLIC_FORUM = "public_forum"


class OperatorFit(str, Enum):
    FOUNDER_LED_NEW_VENTURE = "founder_led_new_venture"
    SMALL_EXPANSION = "small_expansion"
    FRANCHISE_OUTLET = "franchise_outlet"
    MATURE_CHAIN = "mature_chain"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PublicLaunchSignal:
    """One source-bound observation before privacy reduction.

    ``discovered_phone`` exists only to prove the reduction boundary: no output value object has a
    phone field. An adapter may report that a page exposed one; the core does not retain it.
    """

    business_name: str
    city: str
    category: str
    stage: str
    evidence_url: str
    evidence_class: EvidenceClass
    access_date: date
    published_date: date | None = None
    operator_fit: OperatorFit = OperatorFit.UNKNOWN
    founder_name: str | None = None
    has_business_email: bool = False
    has_social_channel: bool = False
    discovered_phone: str | None = None

    def validate(self) -> None:
        for field_name in ("business_name", "city", "category", "stage"):
            if not _clean(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be non-empty")
        parsed = urlparse(self.evidence_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("evidence_url must be an absolute https URL")
        if self.published_date is not None and self.published_date > self.access_date:
            raise ValueError("published_date cannot be after access_date")


@dataclass(frozen=True, slots=True)
class ProspectArtifact:
    prospect_key: str
    business_name: str
    founder_name: str | None
    city: str
    category: str
    stage: str
    evidence_url: str
    evidence_class: str
    access_date: str
    contact_channels_available: tuple[str, ...]
    phone_status: str
    why_now: str
    score: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: str | None) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip())


def _stable_key(signal: PublicLaunchSignal) -> str:
    identity = f"{_clean(signal.business_name).casefold()}|{_clean(signal.city).casefold()}"
    return f"prospect_{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def _age_days(signal: PublicLaunchSignal) -> int | None:
    if signal.published_date is None:
        return None
    return (signal.access_date - signal.published_date).days


def _effective_stage(signal: PublicLaunchSignal) -> str:
    stage = _clean(signal.stage).casefold().replace(" ", "_")
    age = _age_days(signal)
    if age is not None and age > 90 and stage not in {"launched_revalidate", "unknown"}:
        return "launch_signal_revalidate"
    return stage if stage in _STAGE_POINTS else "unknown"


def _score(signal: PublicLaunchSignal, stage: str) -> int:
    score = _STAGE_POINTS[stage]
    score += {
        OperatorFit.FOUNDER_LED_NEW_VENTURE: 25,
        OperatorFit.SMALL_EXPANSION: 18,
        OperatorFit.FRANCHISE_OUTLET: 10,
        OperatorFit.MATURE_CHAIN: 4,
        OperatorFit.UNKNOWN: 8,
    }[signal.operator_fit]
    score += {
        EvidenceClass.GOVERNMENT: 20,
        EvidenceClass.OPERATOR_OWNED: 20,
        EvidenceClass.FOUNDER_OWNED: 20,
        EvidenceClass.REPUTABLE_SECONDARY: 12,
        EvidenceClass.PUBLIC_FORUM: 5,
    }[signal.evidence_class]
    age = _age_days(signal)
    if age is None:
        score += 3
    elif age <= 30:
        score += 10
    elif age <= 90:
        score += 6
    # An available public channel is discovery metadata, never contact authority.
    if signal.has_business_email:
        score += 10
    elif signal.has_social_channel:
        score += 7
    completeness = sum(
        bool(_clean(value))
        for value in (signal.business_name, signal.city, signal.category, signal.stage)
    )
    score += round(completeness / 4 * 10)
    return min(score, 100)


def _why_now(signal: PublicLaunchSignal, stage: str) -> str:
    place = _clean(signal.city)
    category = _clean(signal.category)
    if stage == "launch_signal_revalidate":
        return (
            f"The public {category} launch signal for {place} is older than 90 days; revalidate "
            "current stage before any qualification."
        )
    if stage == "launched_revalidate":
        return f"The stated launch date has passed; verify whether the {category} venture in {place} is live."
    if stage == "newly_incorporated":
        return f"A newly incorporated {category} venture in {place} is still early enough for feasibility work."
    if stage == "pilot":
        return f"The {category} pilot in {place} can still test demand, pricing and operating assumptions."
    if stage == "live_waitlist":
        return f"A live {category} waitlist in {place} creates a pre-launch validation window."
    if stage == "preopening_hiring":
        return f"Pre-opening hiring for the {category} venture in {place} indicates an active launch window."
    if stage == "opening_soon":
        return f"The {category} venture publicly says it is opening soon in {place}."
    return f"The current launch stage of the {category} venture in {place} needs qualification."


def build_prospect(signal: PublicLaunchSignal) -> ProspectArtifact:
    signal.validate()
    stage = _effective_stage(signal)
    channels: list[str] = []
    if signal.has_business_email:
        channels.append("business_email")
    if signal.has_social_channel:
        channels.append("social")
    return ProspectArtifact(
        prospect_key=_stable_key(signal),
        business_name=_clean(signal.business_name),
        founder_name=_clean(signal.founder_name) or None,
        city=_clean(signal.city),
        category=_clean(signal.category),
        stage=stage,
        evidence_url=signal.evidence_url,
        evidence_class=signal.evidence_class.value,
        access_date=signal.access_date.isoformat(),
        contact_channels_available=tuple(channels),
        phone_status="not_contactable_without_consent_basis",
        why_now=_why_now(signal, stage),
        score=_score(signal, stage),
    )


def build_prospect_list(
    signals: Iterable[PublicLaunchSignal], *, limit: int = 50
) -> tuple[ProspectArtifact, ...]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    by_key: dict[str, ProspectArtifact] = {}
    for signal in signals:
        artifact = build_prospect(signal)
        existing = by_key.get(artifact.prospect_key)
        if existing is None or artifact.score > existing.score:
            by_key[artifact.prospect_key] = artifact
    ranked = sorted(
        by_key.values(),
        key=lambda item: (-item.score, item.business_name.casefold(), item.city.casefold()),
    )
    return tuple(ranked[:limit])


def signal_from_mapping(raw: Mapping[str, Any]) -> PublicLaunchSignal:
    """Strict adapter for ACF/context JSON. Unknown/malformed enums fail loud."""

    published_raw = raw.get("published_date")
    return PublicLaunchSignal(
        business_name=str(raw.get("business_name", "")),
        city=str(raw.get("city", "")),
        category=str(raw.get("category", "")),
        stage=str(raw.get("stage", "")),
        evidence_url=str(raw.get("evidence_url", "")),
        evidence_class=EvidenceClass(str(raw.get("evidence_class", ""))),
        access_date=date.fromisoformat(str(raw.get("access_date", ""))),
        published_date=date.fromisoformat(str(published_raw)) if published_raw else None,
        operator_fit=OperatorFit(str(raw.get("operator_fit", OperatorFit.UNKNOWN.value))),
        founder_name=str(raw["founder_name"]) if raw.get("founder_name") else None,
        has_business_email=raw.get("has_business_email") is True,
        has_social_channel=raw.get("has_social_channel") is True,
        discovered_phone=str(raw["discovered_phone"]) if raw.get("discovered_phone") else None,
    )


__all__ = [
    "AGENT_NAME",
    "AGENT_TOOLS",
    "EvidenceClass",
    "OperatorFit",
    "ProspectArtifact",
    "PublicLaunchSignal",
    "build_prospect",
    "build_prospect_list",
    "signal_from_mapping",
]

