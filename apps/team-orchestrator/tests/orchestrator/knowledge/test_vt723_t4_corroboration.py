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


def test_all_sources_pass_real_vt710_pipeline_and_raw_stays_local(artifacts) -> None:
    parent, sources, candidates, _delta = artifacts
    assert len(sources) == len(candidates) == 33
    assert len({source.independence_cluster for source in sources}) == 33
    assert all(source.source_class in {"t1", "t1v", "t2", "t3"} for source in sources)
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
        parent_id = str(
            uuid5(NAMESPACE_URL, f"viabe:o8:card:{source.supports[0].legacy_id}")
        )
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

        # The card is a new source for an existing semantic claim. Its identity and applicability
        # therefore match that claim, not a legacy artifact ID or a generic evidence predicate.
        assert candidate.claim_key.subject == target.claim_key.subject
        assert candidate.claim_key.predicate == target.claim_key.predicate
        assert candidate.claim_key.predicate != "independent_evidence"
        if candidate.source_class is not SourceClass.T1_REGULATORY:
            assert candidate.applicability == target.applicability


def test_delta_accounts_for_every_t4_claim_and_real_negative_findings(artifacts) -> None:
    _parent, _sources, _candidates, delta = artifacts
    assert len(delta) == 18
    assert Counter(row.resolved_status.value for row in delta) == {
        "candidate": 15,
        "disputed": 1,
        "research_only": 2,
    }
    disputed = next(row for row in delta if row.resolved_status.value == "disputed")
    assert disputed.legacy_id == "bk028-comment-sample-loop-for-service-demand"
    assert {edge.stance.value for edge in disputed.evidence_edges} == {
        "corroborates",
        "refutes",
    }
    unresolved = {row.legacy_id: row for row in delta if row.search.recorded_absence}
    assert set(unresolved) == {
        "bk025-high-trust-b2b-free-diagnostic-to-paid-pilot",
        "bk113-good-glamm-integration-capacity",
    }
    assert all(row.search.skipped_paywalled_sources for row in unresolved.values())


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

    duplicate = deepcopy(jsonl("t4_corroboration_sources.jsonl"))
    duplicate[1]["independence_cluster"] = duplicate[0]["independence_cluster"]
    duplicate[1]["underlying_evidence_id"] = duplicate[0]["underlying_evidence_id"]
    with pytest.raises(CorroborationError, match="retellings must collapse"):
        load_source_manifest(duplicate)

    weakened = deepcopy(jsonl("t4_corroboration_delta.jsonl"))
    candidate = next(row for row in weakened if row["resolved_status"] == "candidate")
    candidate["evidence_edges"] = candidate["evidence_edges"][:1]
    candidate["qualifying_new_cluster_count"] = 1
    candidate["total_independence_cluster_count"] = 2
    with pytest.raises(CorroborationError, match="two new corroboration clusters"):
        load_delta(weakened)


def test_plan_changes_evidence_state_not_expression_or_serving(artifacts) -> None:
    parent, sources, candidates, delta = artifacts
    plan = build_corroboration_plan(parent, sources, candidates, delta)
    assert len(plan.members) == 118
    assert len(plan.transitions) == 16
    assert plan.candidate_count == 15
    assert plan.disputed_count == 1
    assert len(plan.unresolved_legacy_ids) == 2
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
    assert sum("INSERT INTO public.knowledge_lifecycle_events" in query for query in queries) == 16
    assert sum("INSERT INTO public.knowledge_corpus_members" in query for query in queries) == 118
    source_edges = [
        params
        for query, params in conn.calls
        if "INSERT INTO public.knowledge_card_sources" in query
    ]
    manifest_clusters = {source.independence_cluster for source in sources}
    assert manifest_clusters <= {params[3] for params in source_edges}  # type: ignore[index]

    source_by_id = {source.source_id: source for source in sources}
    refuting_ids = {
        source.source_id
        for source in sources
        if any(support.stance.value == "refutes" for support in source.supports)
    }
    assert refuting_ids
    persisted_support = {
        params[2]: params[4]  # type: ignore[index]
        for params in source_edges
        if params[2] in source_by_id  # type: ignore[index]
    }
    assert all(persisted_support[source_id] is False for source_id in refuting_ids)
