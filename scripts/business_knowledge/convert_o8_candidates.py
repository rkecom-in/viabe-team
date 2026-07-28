#!/usr/bin/env python3
"""Convert the audited 118-card archive into inert O8 candidate artifacts (VT-710).

This is a deterministic migration utility, not the retired L4 loader. It performs a complete
source-level rights inventory before invoking the ingestion pipeline for any card. Public access is
not treated as a licence: only locally-authored synthesis receives owner-granted content-use rights;
the five audit-identified live links remain ``live_link_only`` and all unverified third-party rights
remain ``unknown``. Every output card is candidate/research-only and retrieval-ineligible.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_SRC = REPO_ROOT / "apps" / "team-orchestrator" / "src"
if str(ORCHESTRATOR_SRC) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_SRC))

from orchestrator.knowledge.contracts import (  # noqa: E402
    Applicability,
    ClaimValueType,
    EvidenceAuthority,
    KnowledgeDomain,
    SourceClass,
    TypedClaimValue,
    UsageRights,
    UsageRightsStatus,
    suggested_confidence_for_source,
)
from orchestrator.knowledge.ingestion import (  # noqa: E402
    AcquiredSource,
    AcquiredContentKind,
    CandidateGovernance,
    EmbeddingMode,
    ExtractedClaimDraft,
    InMemoryCandidateRegistry,
    InMemoryDedupeStore,
    IngestionPipeline,
    MappingRightsResolver,
    QuarantineRecord,
    SourceRightsDecision,
)

INPUT = REPO_ROOT / "archives/business-knowledge/extracted/scenario_cards/executional_scenarios.jsonl"
HISTORICAL_MANIFEST = (
    REPO_ROOT / "archives/business-knowledge/research/HISTORICAL_BUSINESS_CASES_SOURCE_MANIFEST.csv"
)
OUTPUT_DIR = REPO_ROOT / "apps/team-orchestrator/knowledge_corpus"
RIGHTS_OUTPUT = OUTPUT_DIR / "source_rights.jsonl"
CANDIDATE_OUTPUT = OUTPUT_DIR / "candidate_cards.jsonl"
REPORT_OUTPUT = OUTPUT_DIR / "CONVERSION_REPORT.md"

REVIEWED_AT = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
DOMAIN_MAP = {
    "manager_coo": KnowledgeDomain.MANAGEMENT,
    "sales": KnowledgeDomain.SALES,
    "marketing": KnowledgeDomain.MARKETING,
    "compliance": KnowledgeDomain.COMPLIANCE,
    "finance": KnowledgeDomain.FINANCE,
    "accounting": KnowledgeDomain.ACCOUNTING,
    "operations": KnowledgeDomain.OPERATIONS,
    "technology": KnowledgeDomain.TECHNOLOGY,
}


class LegacyScenarioExtractor:
    """Parse only claim content; privileged metadata in the JSON is structurally ignored."""

    tools_enabled: Literal[False] = False

    def extract(self, raw_text: str) -> ExtractedClaimDraft:
        row = json.loads(raw_text)
        note_parts = (
            ("Situation", row["situation"]),
            ("Risk", row["mistake_or_risk"]),
            ("Action", row["recommended_next_action"]),
            ("Evidence", "; ".join(row["evidence_needed"])),
            ("Red flags", "; ".join(row["red_flags"])),
        )
        note = "\n".join(f"{label}: {value}" for label, value in note_parts)
        if len(note) > 4_000:
            note = note[:3_985].rstrip() + " [truncated]"
        subject = row["tags"][0] if row["tags"] else row["title"]
        predicate = row["hard_gate_candidate"]
        if len(predicate) > 200:
            predicate = predicate[:200].rsplit(" ", 1)[0]
        return ExtractedClaimDraft(
            claim=row["agent_lesson"],
            distillation_note=note,
            claim_subject=subject,
            claim_predicate=predicate,
            claim_value=TypedClaimValue(
                value_type=ClaimValueType.TEXT, value=row["recommended_next_action"]
            ),
        )


class ArchiveReferenceQuarantine:
    """Raw corpus already lives in the archive; retain a non-retrievable line reference."""

    def put(self, source: AcquiredSource, *, content_hash: str) -> QuarantineRecord:
        return QuarantineRecord(
            quarantine_ref=f"archive://{source.locator}",
            source_id=source.source_id,
            content_hash=content_hash,
            acquired_at=source.acquired_at,
        )


def _read_cards() -> list[dict[str, Any]]:
    return [json.loads(line) for line in INPUT.read_text(encoding="utf-8").splitlines() if line]


def _live_link_case_ids() -> set[str]:
    with HISTORICAL_MANIFEST.open(newline="", encoding="utf-8") as handle:
        return {
            case_id.strip().casefold()
            for row in csv.DictReader(handle)
            if row["status"] == "live_link_only"
            for case_id in row["case_ids"].split(";")
        }


def _source_class(source_type: str) -> SourceClass:
    value = source_type.casefold()
    if any(token in value for token in ("forum", "discussion", "anonymous")):
        return SourceClass.T4_EXPERIENTIAL
    if "reporting" in value and not any(
        token in value for token in ("official", "primary_company", "regulatory_filing")
    ):
        return SourceClass.T4_EXPERIENTIAL
    if "platform_policy" in value or "platform_guidance" in value:
        return SourceClass.T1_VENDOR_POLICY
    if any(
        token in value
        for token in (
            "official",
            "regulatory",
            "government",
            "nasa",
            "military",
            "emergency_management",
        )
    ):
        return SourceClass.T1_REGULATORY
    if any(
        token in value
        for token in (
            "academic",
            "empirical",
            "experiment",
            "administrative_data",
            "natural_experiment",
            "quasi_experimental",
            "longitudinal",
            "multi_study",
            "systematic_literature",
            "theory_simulation",
            "payment_outage_event",
        )
    ):
        return SourceClass.T2_EVIDENCE
    return SourceClass.T3_PRACTITIONER


def _jurisdiction(card: dict[str, Any], source_class: SourceClass) -> tuple[str, ...]:
    searchable = " ".join(
        (
            str(card["title"]),
            str(card["source_url"]),
            " ".join(str(value) for value in card["tags"]),  # type: ignore[union-attr]
        )
    ).casefold()
    if any(
        token in searchable
        for token in (
            "india",
            "gst",
            "msme",
            "rbi.org.in",
            "pib.gov.in",
            "cert-in.org.in",
            "cci.gov.in",
        )
    ):
        return ("IN",)
    host = urlparse(str(card["source_url"])).hostname or ""
    if host.endswith(".gov") or host.endswith(".mil") or "nasa.gov" in host or "sba.gov" in host:
        return ("US",)
    if host.endswith(".gov.uk"):
        return ("GB",)
    return ("GLOBAL",) if source_class is SourceClass.T1_REGULATORY else ()


def _channels(card: dict[str, Any]) -> tuple[str, ...]:
    values = {str(value).casefold() for value in card["tags"]}  # type: ignore[union-attr]
    mappings = {
        "whatsapp": "whatsapp",
        "email": "email",
        "ecommerce": "ecommerce",
        "digital": "digital",
        "offline": "physical",
        "local_marketing": "physical",
        "b2b": "b2b",
    }
    return tuple(sorted({channel for tag, channel in mappings.items() if tag in values}))


def _domain(card: dict[str, Any]) -> KnowledgeDomain:
    values = [str(value) for value in card["domains"]]  # type: ignore[union-attr]
    for value in values:
        if value != "manager_coo" and value in DOMAIN_MAP:
            return DOMAIN_MAP[value]
    return DOMAIN_MAP.get(values[0], KnowledgeDomain.CROSS_FUNCTIONAL)


def _publisher(url: str) -> str:
    return urlparse(url).hostname or "RKECOM local synthesis"


def _source_inventory_hash(
    source_url: str, source_cards: list[dict[str, Any]]
) -> tuple[str, str]:
    parts = [f"url:{source_url}"]
    archived = 0
    for relative in sorted({str(card["local_file"]) for card in source_cards}):
        path = REPO_ROOT / relative
        if path.is_file():
            archived += 1
            parts.append(f"file:{relative}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    basis = "canonical_url_plus_archived_files" if archived else "canonical_url_only"
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest(), basis


def _add_six_months(value: datetime) -> datetime:
    month_index = value.month - 1 + 6
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    import calendar

    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _rights_for_source(*, is_local_owned: bool, live_link_only: bool) -> UsageRights:
    if live_link_only:
        return UsageRights(
            status=UsageRightsStatus.LIVE_LINK_ONLY,
            reviewed_at=REVIEWED_AT,
            reviewed_by="rights-triage:vt710-audit-manifest",
        )
    if is_local_owned:
        return UsageRights(
            status=UsageRightsStatus.PERMISSION_GRANTED,
            allows_extraction=True,
            allows_embedding=True,
            allows_retrieval=True,
            reviewed_at=REVIEWED_AT,
            reviewed_by="rkecom-source-owner:vt710",
        )
    return UsageRights(
        status=UsageRightsStatus.UNKNOWN,
        reviewed_at=REVIEWED_AT,
        reviewed_by="rights-triage:vt710-unverified",
    )


def _build_source_inventory(
    cards: list[dict[str, Any]], live_link_ids: set[str]
) -> tuple[list[dict[str, Any]], dict[str, SourceRightsDecision]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        grouped[str(card["source_url"])].append(card)

    rows: list[dict[str, Any]] = []
    decisions: dict[str, SourceRightsDecision] = {}
    for source_url, source_cards in sorted(grouped.items()):
        source_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:source:{source_url}"))
        source_types = {str(card["source_type"]) for card in source_cards}
        is_local_owned = source_url.startswith("archives/business-knowledge/")
        if is_local_owned:
            source_class = SourceClass.T3_PRACTITIONER
        else:
            source_classes = {_source_class(value) for value in source_types}
            if len(source_classes) != 1:
                raise ValueError(f"one remote source has conflicting source classes: {source_url}")
            source_class = next(iter(source_classes))
        case_live_link = any(
            any(
                str(card["id"]).casefold() == case_id
                or str(card["id"]).casefold().startswith(f"{case_id}-")
                for case_id in live_link_ids
            )
            for card in source_cards
        )
        rights = _rights_for_source(
            is_local_owned=is_local_owned, live_link_only=case_live_link
        )
        acquired_at = min(
            datetime.fromisoformat(f"{card['retrieved_at']}T00:00:00+00:00")
            for card in source_cards
        )
        content_hash, hash_basis = _source_inventory_hash(source_url, source_cards)
        decision = SourceRightsDecision(
            source_class=source_class,
            usage_rights=rights,
        )
        decisions[source_id] = decision
        rows.append(
            {
                "source_id": source_id,
                "canonical_url": source_url,
                "publisher": _publisher(source_url),
                "source_class": source_class.value,
                "content_hash": content_hash,
                "content_hash_basis": hash_basis,
                "acquired_at": acquired_at.isoformat().replace("+00:00", "Z"),
                "source_type_inputs": sorted(source_types),
                "card_ids": sorted(str(card["id"]) for card in source_cards),
                "local_files": sorted({str(card["local_file"]) for card in source_cards}),
                "usage_rights": rights.model_dump(mode="json"),
                "retention_class": "lifecycle_managed",
                "tainted": True,
                "expires_at": (
                    _add_six_months(acquired_at).isoformat().replace("+00:00", "Z")
                    if source_class is SourceClass.T4_EXPERIENTIAL
                    else None
                ),
                "rights_pass_completed_before_conversion": True,
            }
        )
    return rows, decisions


def convert() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cards = _read_cards()
    if len(cards) != 118 or len({str(card["id"]) for card in cards}) != 118:
        raise ValueError("VT-710 conversion requires exactly 118 unique audited cards")

    live_link_ids = _live_link_case_ids()
    if len(live_link_ids) != 5:
        raise ValueError("historical source manifest must contain exactly five live-link cards")

    # The entire rights inventory is completed and cardinality-checked before card 1 is converted.
    rights_rows, decisions = _build_source_inventory(cards, live_link_ids)
    if len(rights_rows) != len({str(card["source_url"]) for card in cards}):
        raise ValueError("rights inventory does not cover every unique source")

    registry = InMemoryCandidateRegistry()
    pipeline = IngestionPipeline(
        rights=MappingRightsResolver(decisions),
        quarantine=ArchiveReferenceQuarantine(),
        dedupe=InMemoryDedupeStore(),
        extractor=LegacyScenarioExtractor(),
        registry=registry,
        embedder=None,
        embedding_mode=EmbeddingMode.DEFER,
    )

    candidates: list[dict[str, Any]] = []
    for line_number, card in enumerate(cards, start=1):
        source_url = str(card["source_url"])
        source_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:source:{source_url}"))
        raw_text = json.dumps(card, sort_keys=True, separators=(",", ":"))
        legacy_id = str(card["id"])
        source_decision = decisions[source_id]
        retrieved_at = datetime.fromisoformat(f"{card['retrieved_at']}T00:00:00+00:00")
        applicability = Applicability(
            jurisdictions=_jurisdiction(card, source_decision.source_class),
            channels=_channels(card),
            effective_from=(
                retrieved_at
                if source_decision.source_class is SourceClass.T1_REGULATORY
                else None
            ),
        )
        candidate = pipeline.ingest(
            AcquiredSource(
                source_id=source_id,
                canonical_url=source_url,
                publisher=_publisher(source_url),
                acquired_at=retrieved_at,
                raw_text=raw_text,
                locator=f"{INPUT.relative_to(REPO_ROOT)}#line={line_number}",
                content_kind=AcquiredContentKind.OWNED_DISTILLATION,
            ),
            governance=CandidateGovernance(
                domain=_domain(card),
                authority=EvidenceAuthority.SEED,
                confidence=suggested_confidence_for_source(source_decision.source_class),
                applicability=applicability,
                retention_class="lifecycle_managed",
                independence_cluster=f"source:{source_id}",
                expires_at=(
                    _add_six_months(retrieved_at)
                    if source_decision.source_class is SourceClass.T4_EXPERIENTIAL
                    else None
                ),
            ),
            card_id=str(uuid5(NAMESPACE_URL, f"viabe:o8:card:{legacy_id}")),
            card_version_id=str(uuid5(NAMESPACE_URL, f"viabe:o8:card-version:{legacy_id}:1")),
        )
        warnings: list[str] = []
        if candidate.embedding_state.value == "rights_blocked":
            warnings.append("rights_blocked_no_embedding_or_retrieval")
        elif candidate.embedding_state.value == "pending":
            warnings.append("embedding_deferred_until_authorized_egress")
        if candidate.card.source_class is SourceClass.T1_REGULATORY:
            warnings.append("effective_from_is_observation_date_pending_authoritative_review")
        candidates.append(
            {
                "legacy_id": legacy_id,
                "source_id": source_id,
                "source_url": source_url,
                "additional_domains": [str(value) for value in card["domains"]],  # type: ignore[union-attr]
                "card": candidate.card.model_dump(mode="json"),
                "source_content_hash": candidate.source_content_hash,
                "quarantine_ref": candidate.quarantine_ref,
                "embedding_state": candidate.embedding_state.value,
                "pipeline_steps": list(candidate.pipeline_steps),
                "conversion_warnings": warnings,
            }
        )
    if len(registry.candidates) != 118:
        raise ValueError("candidate registry did not receive all 118 cards")
    return rights_rows, candidates


def _write_outputs(
    rights_rows: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RIGHTS_OUTPUT.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rights_rows),
        encoding="utf-8",
    )
    CANDIDATE_OUTPUT.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in candidates),
        encoding="utf-8",
    )
    rights_counts = Counter(row["usage_rights"]["status"] for row in rights_rows)  # type: ignore[index]
    card_statuses = Counter(row["card"]["status"] for row in candidates)  # type: ignore[index]
    embedding_states = Counter(str(row["embedding_state"]) for row in candidates)
    report = f"""# VT-710 O8 corpus conversion report

Generated deterministically from `executional_scenarios.jsonl` on 2026-07-27.

- Input cards: **{len(candidates)}**
- Unique source-rights records: **{len(rights_rows)}**
- Rights statuses: `{dict(sorted(rights_counts.items()))}`
- Card statuses: `{dict(sorted(card_statuses.items()))}`
- Embedding states: `{dict(sorted(embedding_states.items()))}`
- Retrieval-eligible cards: **{sum(bool(row['card']['retrieval_eligible']) for row in candidates)}**

The rights inventory was completed before conversion began. Public accessibility was not treated
as a licence. Third-party sources without an explicit grant remain `unknown`; the five audited
unarchived sources remain `live_link_only`. Only RKECOM-authored local synthesis has
`permission_granted`, and its embedding remains deferred because VT-710 has no egress authority.
All cards remain candidate/research-only and are consumed by no live route.

Claim keys use the audited primary topic plus normalized hard-gate mechanism, jurisdiction,
population and channel. They are deterministic candidate keys, not a human finding that two cards
are comparable; domain review remains mandatory before validation/admission.
"""
    REPORT_OUTPUT.write_text(report, encoding="utf-8")


def main() -> int:
    rights_rows, candidates = convert()
    _write_outputs(rights_rows, candidates)
    print(
        f"VT-710 conversion complete: {len(candidates)} cards, "
        f"{len(rights_rows)} source-rights records"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
