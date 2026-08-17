"""VT-723: independent evidence resolution of the 18 T4 forum claims."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.contracts import CardStatus, EvidenceAuthority, SourceClass
from orchestrator.knowledge.ingestion import CandidateArtifact
from orchestrator.knowledge.persisted_embeddings import card_content_digest
from orchestrator.knowledge.registry_full import build_full_plan, load_independence_audit
from orchestrator.knowledge.registry_resolution import (
    build_resolution_plan,
    load_resolution_delta,
)
from orchestrator.knowledge.t4_corroboration import (
    CorroborationError,
    build_corroboration_plan,
    load_delta,
    load_source_manifest,
    persist_corroboration_plan,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS = ROOT / "apps" / "team-orchestrator" / "knowledge_corpus"
PIPELINE_STEPS = (
    "source_governance_recorded",
    "hashed_deduped",
    "raw_quarantined",
    "toolless_extracted",
    "schema_validated",
    "expression_originality_checked",
    "claim_applicability_normalized",
    "pii_redacted",
    "global_purity_checked",
    "independence_cluster_bound",
    "embedding_deferred_inert",
    "candidate_registered",
)


def jsonl(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (CORPUS / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture(scope="module")
def artifacts():
    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    full = build_full_plan(
        jsonl("source_rights.jsonl"),
        jsonl("candidate_cards.jsonl"),
        audit,
    )
    parent = build_resolution_plan(
        full,
        load_resolution_delta(jsonl("deferral_resolution_delta.jsonl")),
    )
    sources = load_source_manifest(jsonl("t4_corroboration_sources.jsonl"))
    candidates = tuple(
        CandidateArtifact.model_validate(row) for row in jsonl("t4_corroboration_candidates.jsonl")
    )
    delta = load_delta(jsonl("t4_corroboration_delta.jsonl"))
    return parent, sources, candidates, delta


def test_unverified_cards_cannot_be_loaded_and_the_boolean_is_not_trusted() -> None:
    """The gate's MECHANISM, tested on synthetic rows so it stays true as the corpus is fixed.

    Reading the 33 archived sources found that only 6 cards are faithful citations. Under
    CL-2026-08-13 the other 27 may enter only as explicitly disclosed T4 judgment — and the
    enforcement has to be a gate rather than a sentence in a report, because the previous version
    asserted its posture in a report while `--execute` would have written regardless.
    """
    from orchestrator.knowledge.t4_corroboration import (
        EXPECTED_SOURCE_CARDS,
        assert_corpus_verified,
    )

    def row(index: int, **overrides: object) -> dict[str, Any]:
        base: dict[str, Any] = {
            "index": index,
            "card_version_id": f"card-{index}",
            "source_id": f"source-{index}",
            "source_title": "A governed primary source",
            "local_archive_path": "archives/business-knowledge/research/x/y.html",
            "source_class_before_correction": "t2",
            "recorded_source_class": "t2",
            "recorded_confidence": "medium",
            "recorded_jurisdictions": (),
            "claim_verdict": "SUPPORTED",
            "vanish_verdict": "CITATION",
            "action_in_source": "SOURCE_STATES_IT",
            "tier_verdict": "TIER_OK",
            "jurisdiction_verdict": "OK",
            "confidence_verdict": "OK",
            "pre_claim_verdict": "SUPPORTED",
            "pre_vanish_verdict": "CITATION",
            "pre_action_in_source": "SOURCE_STATES_IT",
            "pre_tier_verdict": "TIER_OK",
            "pre_jurisdiction_verdict": "OK",
            "pre_confidence_verdict": "OK",
            "correction_action": "RETAINED_EARNED_CITATION_TIER",
            "waiver": False,
            "landing_grade": True,
            "notes": "verified against the archived bytes",
        }
        base.update(overrides)
        return base

    def expected(items: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
        return {
            item["card_version_id"]: (
                item["recorded_source_class"],
                item["source_id"],
            )
            for item in items
        }

    clean = [row(index) for index in range(EXPECTED_SOURCE_CARDS)]
    assert len(assert_corpus_verified(clean, expected(clean))) == EXPECTED_SOURCE_CARDS

    demoted = [row(index) for index in range(EXPECTED_SOURCE_CARDS)]
    demoted[7] = row(
        7,
        recorded_source_class="t4",
        claim_verdict="JUDGMENT_DISCLOSED",
        vanish_verdict="JUDGMENT",
        action_in_source="TIERED_ACTION_DROPPED",
        correction_action="DEMOTED_TO_T4",
    )
    assert len(assert_corpus_verified(demoted, expected(demoted))) == EXPECTED_SOURCE_CARDS

    # Each verdict dimension independently blocks the load. A card whose claim is not in its source,
    # or that is really the author's judgment, or whose tier belongs to a different artifact, or
    # that claims a jurisdiction its source has no authority over, is not loadable.
    for field, value in (
        ("claim_verdict", "NOT_FOUND"),
        ("vanish_verdict", "JUDGMENT"),
        ("tier_verdict", "TIER_SHOULD_BE_t3"),
        ("jurisdiction_verdict", "IN_WRONG"),
        ("confidence_verdict", "HIGH_TO_MEDIUM"),
    ):
        broken = [row(index) for index in range(EXPECTED_SOURCE_CARDS)]
        broken[7] = row(7, **{field: value})
        with pytest.raises(CorroborationError, match="not verified against their sources"):
            assert_corpus_verified(broken, expected(broken))

    # The stored `landing_grade` boolean is recomputed, never trusted — otherwise the cheapest way
    # past the gate would be to edit one word in a data file.
    forged = [row(index) for index in range(EXPECTED_SOURCE_CARDS)]
    forged[3] = row(3, claim_verdict="NOT_FOUND", landing_grade=True)
    with pytest.raises(CorroborationError, match="not verified against their sources"):
        assert_corpus_verified(forged, expected(forged))

    waived = [row(index) for index in range(EXPECTED_SOURCE_CARDS)]
    waived[2] = row(2, waiver=True)
    with pytest.raises(CorroborationError, match="not verified against their sources"):
        assert_corpus_verified(waived, expected(waived))

    # A verification row cannot self-declare T4 while the generated card still carries T2.
    unbound = [row(index) for index in range(EXPECTED_SOURCE_CARDS)]
    unbound[4] = row(
        4,
        recorded_source_class="t4",
        claim_verdict="JUDGMENT_DISCLOSED",
        vanish_verdict="JUDGMENT",
        action_in_source="TIERED_ACTION_DROPPED",
        correction_action="DEMOTED_TO_T4",
    )
    with pytest.raises(CorroborationError, match="not bound to the generated candidate corpus"):
        assert_corpus_verified(unbound, expected(clean))

    # A truncated record cannot pass by omitting the cards that fail.
    with pytest.raises(CorroborationError, match="exactly 33 unique cards"):
        assert_corpus_verified(clean[:6], expected(clean))


def test_the_committed_verification_record_reflects_the_real_corpus_state() -> None:
    """Documents what the source reading found, and keeps the gate honest about today.

    Deliberately NOT an assertion that N cards fail — that would have to be edited every time a
    card is fixed, and a test nobody can satisfy gets deleted. It asserts the record COVERS every
    card and that the gate's verdict follows the record, so whichever way the corpus moves, the
    load gate and the evidence stay in agreement.
    """
    from orchestrator.knowledge.t4_corroboration import (
        EXPECTED_SOURCE_CARDS,
        SourceVerificationRow,
        assert_corpus_verified,
    )

    rows = jsonl("t4_corroboration_verification.jsonl")
    parsed = [SourceVerificationRow.model_validate(item) for item in rows]
    assert len(parsed) == EXPECTED_SOURCE_CARDS
    cards = {item["card"]["card_version_id"] for item in jsonl("t4_corroboration_candidates.jsonl")}
    assert {item.card_version_id for item in parsed} == cards, (
        "every card must carry a verdict; a card with no verdict is an unverified card"
    )
    assert all(item.landing_grade == item.verified for item in parsed), (
        "the stored boolean must agree with the verdicts it is derived from"
    )
    assert all(item.verified for item in parsed)
    assert all(item.waiver is False for item in parsed)
    expected_cards = {
        item["card"]["card_version_id"]: (
            item["card"]["source_class"],
            item["card"]["provenance"]["source_ids"][0],
        )
        for item in jsonl("t4_corroboration_candidates.jsonl")
    }
    assert_corpus_verified(rows, expected_cards)


def _builder():
    """The build script, imported for its committed SOURCES table. Import is side-effect free."""

    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.business_knowledge import build_o8_t4_corroboration as module

    return module


def test_no_card_carries_a_predicate_or_a_decision_its_source_never_gave(artifacts) -> None:
    """CL-2026-08-13-judgment-vs-citation, enforced against the committed artifacts.

    The original builder stamped the parent T4 forum card's `claim_key` onto every distillation and
    set `claim_value` to `spec["action"]`. So a card whose claim faithfully cited the CGST Act
    carried a predicate demanding a notice-upload checklist and a value instructing the owner to
    "classify the statutory ground before changing tax treatment" — neither of which the Act says.
    That is judgment riding a source's tier, and it was one builder line, not 33 authoring errors.

    This test needs no archive: the SOURCES table is committed code, so the committed data can be
    checked against the specs it was generated from. That is what makes it runnable in CI, and what
    stops the artifacts and the builder drifting apart.
    """
    parent, sources, candidates, _delta = artifacts
    builder = _builder()

    spec_by_url = {spec["url"]: spec for spec in builder.SOURCES}
    assert len(spec_by_url) == len(builder.SOURCES) == 33
    card_by_version = {candidate.card.card_version_id: candidate.card for candidate in candidates}
    parent_predicates = {
        card.claim_key.predicate
        for card in parent.members
        if card.source_class is SourceClass.T4_EXPERIENTIAL
    }
    assert parent_predicates, "the T4 parents must be present for this to mean anything"

    for source in sources:
        spec = spec_by_url[source.canonical_url]
        card = card_by_version[source.candidate_card_version_id]
        assert card.claim_key.predicate == builder.evidence_predicate(spec["claim"]), (
            f"{source.source_id}: predicate must be derived from the card's OWN claim"
        )
        assert card.claim_key.predicate not in parent_predicates, (
            f"{source.source_id}: inheriting the parent's behavioural predicate is the defect"
        )
        assert card.claim_value.value == card.claim, (
            f"{source.source_id}: a card carrying a source's tier may assert only the cited claim"
        )
        assert card.claim_value.value != spec["action"], (
            f"{source.source_id}: `action` is OUR recommendation, not the source's — it may not "
            "ride the source's tier"
        )
        assert card.distillation_note.startswith("Cited: "), (
            f"{source.source_id}: the note records what the source says, not a decision we made"
        )
        # The spec keeps `action` deliberately, as provenance of what the acquisition was chasing.
        # Asserting it is still there is what keeps the negative assertion above meaningful.
        assert spec["action"], f"{source.source_id}: acquisition intent must stay recorded"

    assert len({card.claim_key.predicate for card in card_by_version.values()}) == 33, (
        "33 distinct claims must yield 33 distinct claim identities"
    )


@pytest.mark.skipif(
    not (ROOT / "archives/business-knowledge/research/vt723-t4-corroboration").is_dir(),
    reason="local-only source archive is unavailable; committed artifacts remain validated",
)
def test_local_source_bytes_verify_and_regeneration_is_deterministic(artifacts) -> None:
    """The verifiability condition of the ruling, made falsifiable instead of asserted.

    `archives/` is gitignored (~200MB of third-party pages, never committed), so this cannot be an
    unconditional test — but "deterministic regeneration: 33 sources" was in the PR body with
    nothing behind it, and the only assertions on the archive were a path prefix and a 64-hex
    shape. Neither would notice if the recorded digest belonged to a different document.

    Two things are checked where the archive exists: the recorded digest IS the sha256 of the real
    acquired bytes, and re-running the builder over those bytes reproduces the committed artifacts
    exactly.
    """
    import hashlib

    _parent, sources, _candidates, _delta = artifacts
    for source in sources:
        path = ROOT / source.local_archive_path
        assert path.is_file(), f"{source.source_id}: recorded archive path does not resolve"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == source.local_archive_sha256 == source.content_hash, (
            f"{source.source_id}: recorded digest is not the digest of these bytes"
        )

    builder = _builder()
    manifests, candidates, delta = builder.build()
    assert manifests == jsonl("t4_corroboration_sources.jsonl")
    assert candidates == jsonl("t4_corroboration_candidates.jsonl")
    assert delta == jsonl("t4_corroboration_delta.jsonl")


def test_all_sources_pass_real_vt710_pipeline_and_raw_stays_local(artifacts) -> None:
    parent, sources, candidates, _delta = artifacts
    assert len(sources) == len(candidates) == 33
    assert len({source.independence_cluster for source in sources}) == 32
    assert Counter(source.source_class for source in sources)["t4"] == 27
    assert all(source.depends_on_original_forum is False for source in sources)
    assert all(source.paywall_access_circumvented is False for source in sources)
    assert all(source.local_archive_path.startswith("archives/") for source in sources)
    assert all(len(source.local_archive_sha256) == 64 for source in sources)
    assert all(source.content_hash == source.local_archive_sha256 for source in sources)
    assert all(candidate.pipeline_steps == PIPELINE_STEPS for candidate in candidates)
    assert all(candidate.expression_originality.mode.value == "checked" for candidate in candidates)
    assert all(
        candidate.expression_originality.scanner == "token-shingle-v1" for candidate in candidates
    )
    assert all(candidate.embedding_state.value == "pending" for candidate in candidates)
    assert all(candidate.card.retrieval_eligible is False for candidate in candidates)

    candidate_by_version = {candidate.card.card_version_id: candidate for candidate in candidates}
    parent_by_card_id = {card.card_id: card for card in parent.members}
    for source in sources:
        candidate = candidate_by_version[source.candidate_card_version_id].card
        parent_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:card:{source.supports[0].legacy_id}"))
        target = parent_by_card_id[parent_id]

        # Provenance is complete per card: the source, acquisition date and source tier are bound
        # to the candidate rather than inferred later from its prose.
        assert candidate.provenance.source_ids == (source.source_id,)
        assert candidate.provenance.publisher == source.publisher
        assert candidate.provenance.retrieved_at == source.acquired_at
        assert candidate.source_class.value == source.source_class

        # Codex-authored distillation is never labelled owner/VTR/verified-human evidence. A fact
        # may retain its primary source's T1/T1v class; authorship authority remains seed.
        assert candidate.authority is EvidenceAuthority.SEED
        assert any(
            (
                candidate.applicability.jurisdictions,
                candidate.applicability.size_bands,
                candidate.applicability.maturity_stages,
                candidate.applicability.channels,
            )
        ), f"{source.source_id}: applicability must never be empty"
        if candidate.source_class is SourceClass.T4_EXPERIENTIAL:
            assert candidate.expires_at is not None

        # The card is a new source for an existing semantic claim, so it shares that claim's
        # SUBJECT — a topic dimension. It must NOT share the parent's PREDICATE: that predicate is
        # a behavioural instruction the T4 forum author gave, and copying it onto a card that
        # carries a primary source's tier is exactly the smuggling CL-2026-08-13 forbids. This
        # assertion used to require the opposite; it was pinning the defect in place.
        assert candidate.claim_key.subject == target.claim_key.subject
        assert candidate.claim_key.predicate != target.claim_key.predicate
        assert candidate.claim_key.predicate != "independent_evidence"
        assert source.source_class_before_correction in {"t1", "t1v", "t2", "t3"}


def test_delta_accounts_for_every_t4_claim_and_real_negative_findings(artifacts) -> None:
    _parent, _sources, _candidates, delta = artifacts
    assert len(delta) == 18
    assert Counter(row.resolved_status.value for row in delta) == {"research_only": 18}
    unresolved = {row.legacy_id: row for row in delta if row.search.recorded_absence}
    assert len(unresolved) == 18
    assert {
        "bk019-bulky-product-shipping-unit-economics-local-first",
        "bk031-partner-network-co-selling-borrows-trust",
        "bk032-brand-building-memory-proof-and-promise",
    } <= set(unresolved)


def test_independence_threshold_excludes_forum_partial_and_semantic_retellings(artifacts) -> None:
    _parent, sources, _candidates, delta = artifacts
    source_by_id = {source.source_id: source for source in sources}
    for row in delta:
        qualifying = {
            edge.independence_cluster for edge in row.evidence_edges if edge.qualifies_for_threshold
        }
        assert row.original_independence_cluster not in qualifying
        assert row.total_independence_cluster_count == 1 + len(qualifying)
        for edge in row.evidence_edges:
            source = source_by_id[edge.source_id]
            assert edge.independence_cluster == source.underlying_evidence_id
            assert source.depends_on_original_forum is False

    cai_szeidl = [
        source
        for source in sources
        if source.local_archive_path.endswith(
            ("nber-firm-referrals-w33082.pdf", "nber-interfirm-w22951.pdf")
        )
    ]
    assert len(cai_szeidl) == 2
    assert len({source.independence_cluster for source in cai_szeidl}) == 1

    # Two separately auditable source rows may share one underlying cluster. Loading both must not
    # reject them; the delta's set-based threshold is where the retelling collapses to one.
    duplicate = deepcopy(jsonl("t4_corroboration_sources.jsonl"))
    duplicate[1]["independence_cluster"] = duplicate[0]["independence_cluster"]
    duplicate[1]["underlying_evidence_id"] = duplicate[0]["underlying_evidence_id"]
    assert len(load_source_manifest(duplicate)) == 33


def test_plan_changes_evidence_state_not_expression_or_serving(artifacts) -> None:
    parent, sources, candidates, delta = artifacts
    plan = build_corroboration_plan(parent, sources, candidates, delta)
    assert len(plan.members) == 118
    assert len(plan.transitions) == 0
    assert plan.candidate_count == 0
    assert plan.disputed_count == 0
    assert len(plan.unresolved_legacy_ids) == 18
    assert plan.corpus_status == "shadow"
    assert plan.admission_verdict == "pending"
    assert plan.AUTHORIZES_EFFECTS is False

    for item in plan.transitions:
        assert item.prior.status is CardStatus.RESEARCH_ONLY
        assert item.resolved.status in {CardStatus.CANDIDATE, CardStatus.DISPUTED}
        assert item.resolved.retrieval_eligible is False
        assert item.resolved.card_version == 2
        assert item.resolved.claim == item.prior.claim
        assert item.resolved.distillation_note == item.prior.distillation_note
        assert card_content_digest(item.resolved) == card_content_digest(item.prior)
        assert item.resolved.corroboration_cluster_count >= 3

    assert sum(card.retrieval_eligible for card in plan.members) == 100
    assert not any(
        card.source_class is SourceClass.T4_EXPERIENTIAL and card.retrieval_eligible
        for card in plan.members
    )


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = ...) -> None:
        self.calls.append((query, params))


def test_persistence_is_append_only_complete_and_uses_real_evidence_clusters(artifacts) -> None:
    parent, sources, candidates, delta = artifacts
    plan = build_corroboration_plan(parent, sources, candidates, delta)
    conn = RecordingConnection()
    persist_corroboration_plan(conn, plan)
    queries = [query for query, _params in conn.calls]
    assert not any(" UPDATE " in f" {query.upper()} " for query in queries)
    assert not any(" DELETE " in f" {query.upper()} " for query in queries)
    assert sum("INSERT INTO public.knowledge_sources" in query for query in queries) == 33
    assert sum("INSERT INTO public.knowledge_corpus_versions" in query for query in queries) == 1
    assert sum("INSERT INTO public.knowledge_lifecycle_events" in query for query in queries) == 0
    assert sum("INSERT INTO public.knowledge_corpus_members" in query for query in queries) == 118
    source_edges = [
        params
        for query, params in conn.calls
        if "INSERT INTO public.knowledge_card_sources" in query
    ]
    manifest_clusters = {source.independence_cluster for source in sources}
    assert manifest_clusters <= {params[3] for params in source_edges}  # type: ignore[index]

    refuting_ids = {
        source.source_id
        for source in sources
        if any(support.stance.value == "refutes" for support in source.supports)
    }
    assert refuting_ids
    assert all(
        not support.qualifies_for_threshold
        for source in sources
        for support in source.supports
        if source.source_id in refuting_ids
    )
