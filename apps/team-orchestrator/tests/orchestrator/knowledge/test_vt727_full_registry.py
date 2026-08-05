"""VT-727 full-corpus gates: no silent drops, inflated authority, or unowned deferrals."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.registry_full import (
    FullRegistryError,
    build_full_plan,
    load_independence_audit,
    persist_full_plan,
    screen_cross_source_pairs,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS = ROOT / "apps" / "team-orchestrator" / "knowledge_corpus"


def _jsonl(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (CORPUS / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


def _inputs():
    rights = _jsonl("source_rights.jsonl")
    candidates = _jsonl("candidate_cards.jsonl")
    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    return rights, candidates, audit


def test_all_118_receive_a_persistable_disposition_and_route_out() -> None:
    plan = build_full_plan(*_inputs())
    assert len(plan.cards) == 118
    assert len({item.legacy_id for item in plan.cards}) == 118
    assert plan.promoted_count == 64
    assert plan.deferred_count == 54
    assert Counter(item.disposition for item in plan.cards) == {
        "shadow_validated": 64,
        "deferred_candidate": 36,
        "deferred_research_only": 18,
    }
    assert all(item.reasons and item.route_out for item in plan.cards)
    assert sum(item.representative.retrieval_eligible for item in plan.cards) == 64
    assert plan.AUTHORIZES_EFFECTS is False
    assert plan.corpus_status == "shadow"
    assert plan.admission_verdict == "pending"


def test_authority_audit_keeps_only_real_platform_policy_at_t1v() -> None:
    rights, candidates, audit = _inputs()
    plan = build_full_plan(rights, candidates, audit)
    assert plan.authority_counts == {"t1": 25, "t1v": 2, "t2": 28, "t3": 45, "t4": 18}
    guidance = [
        row
        for row in rights
        if any("platform_guidance" in value for value in row["source_type_inputs"])
    ]
    policy = [
        row
        for row in rights
        if any("platform_policy" in value for value in row["source_type_inputs"])
    ]
    assert len(guidance) == 3 and {row["source_class"] for row in guidance} == {"t4"}
    assert len(policy) == 2 and {row["source_class"] for row in policy} == {"t1v"}


def test_independence_review_is_complete_digest_bound_and_explicit_none() -> None:
    rights, candidates, audit = _inputs()
    plan = build_full_plan(rights, candidates, audit)
    assert len(screen_cross_source_pairs(candidates)) == 68
    assert len(audit.pair_reviews) == 68
    assert plan.screened_cross_source_pairs == 68
    assert plan.collapsed_retelling_groups == 0
    assert audit.conclusion == "no_cross_source_retellings_found"

    missing_review = audit.__class__(**{**audit.__dict__, "pair_reviews": audit.pair_reviews[:-1]})
    with pytest.raises(FullRegistryError, match="every screened"):
        build_full_plan(rights, candidates, missing_review)

    stale = audit.__class__(**{**audit.__dict__, "corpus_artifact_digest": "0" * 64})
    with pytest.raises(FullRegistryError, match="does not bind"):
        build_full_plan(rights, candidates, stale)


def test_concentration_and_overlapping_deferral_grounds_reconcile() -> None:
    plan = build_full_plan(*_inputs())
    assert plan.largest_source_card_count == 5
    assert plan.largest_source_share == pytest.approx(0.042373)
    assert plan.largest_source_share < 0.10
    grounds = Counter(reason for item in plan.cards for reason in item.reasons)
    assert grounds == {
        "deterministic_shadow_gate_passed": 64,
        "authoritative_effective_date_unverified": 25,
        "experiential_claim_requires_independent_corroboration": 18,
        "originality_attestation_requires_independent_recheck": 13,
        "vendor_policy_currentness_requires_review": 2,
    }


def test_global_purity_is_rechecked_over_all_118_cards_and_104_sources() -> None:
    plan = build_full_plan(*_inputs(), tenant_identifiers=("real-tenant-id-negative-control",))
    assert len(plan.cards) == 118


def test_deferred_candidate_round_trips_with_no_admission_corpus_identity() -> None:
    from orchestrator.knowledge.registry_seed import _card_from_row

    card = next(
        item.representative
        for item in build_full_plan(*_inputs()).cards
        if item.disposition == "deferred_candidate"
    )
    row = {
        "id": card.card_version_id,
        "card_key": card.card_id,
        "version": card.card_version,
        "corpus_version_id": None,
        "claim": card.claim,
        "claim_key": card.claim_key.canonical,
        "claim_value": card.claim_value.model_dump(mode="json"),
        "distillation_note": card.distillation_note,
        "source_class": card.source_class.value,
        "domain": card.domain.value,
        "authority": card.authority.value,
        "confidence": card.confidence.value,
        "independence_cluster": card.independence_cluster,
        "corroboration_cluster_count": card.corroboration_cluster_count,
        "jurisdictions": card.applicability.jurisdictions,
        "size_bands": card.applicability.size_bands,
        "industries": card.applicability.industries,
        "maturity_stages": card.applicability.maturity_stages,
        "channels": card.applicability.channels,
        "applicability_universal": card.applicability.universal,
        "effective_from": card.applicability.effective_from,
        "effective_until": card.applicability.effective_to,
        "provenance": card.provenance.model_dump(mode="json"),
        "usage_rights": card.usage_rights.model_dump(mode="json"),
        "retention_class": card.retention_class,
        "scope": card.scope.value,
        "default_assignment": card.default_assignment,
        "status": card.status.value,
        "retrieval_eligible": card.retrieval_eligible,
        "expires_at": card.expires_at,
    }
    assert _card_from_row(row) == card


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = ...) -> None:
        self.calls.append((query, params))


def test_persistence_writes_full_snapshot_49_new_promotions_and_118_members() -> None:
    plan = build_full_plan(*_inputs())
    conn = RecordingConnection()
    persist_full_plan(conn, plan)
    queries = [query for query, _ in conn.calls]
    assert sum("INSERT INTO public.knowledge_sources" in query for query in queries) == 104
    assert sum("INSERT INTO public.knowledge_cards" in query for query in queries) == 182
    assert sum("INSERT INTO public.knowledge_lifecycle_events" in query for query in queries) == 49
    assert sum("INSERT INTO public.knowledge_corpus_members" in query for query in queries) == 118
    assert sum("INSERT INTO public.knowledge_corpus_versions" in query for query in queries) == 1


def test_committed_disposition_artifact_reconciles_without_card_content() -> None:
    rows = _jsonl("full_ingestion_disposition.jsonl")
    assert len(rows) == 118
    assert Counter(row["disposition"] for row in rows) == {
        "shadow_validated": 64,
        "deferred_candidate": 36,
        "deferred_research_only": 18,
    }
    forbidden = {"claim", "distillation_note", "claim_value", "raw_text"}
    assert all(forbidden.isdisjoint(row) for row in rows)
    assert all(row["reasons"] and row["route_out"] for row in rows)
    assert not any(row["authorizes_effects"] for row in rows)
