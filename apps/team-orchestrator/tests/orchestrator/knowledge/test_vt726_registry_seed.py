from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.contracts import CardStatus
from orchestrator.knowledge.registry_seed import (
    RegistrySeedError,
    SEED_LEGACY_IDS,
    build_seed_plan,
    load_validated_cards,
    persist_seed_plan,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS = ROOT / "apps/team-orchestrator/knowledge_corpus"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def artifacts() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _jsonl(CORPUS / "source_rights.jsonl"), _jsonl(CORPUS / "candidate_cards.jsonl")


def test_seed_is_fixed_balanced_pipeline_output_with_pending_corpus_admission(artifacts):
    plan = build_seed_plan(*artifacts, tenant_identifiers=("real-tenant-id-negative-control",))

    assert len(plan.cards) == 15
    assert {item.legacy_id for item in plan.cards} == SEED_LEGACY_IDS
    assert {item.validated.domain.value for item in plan.cards} == {
        "management",
        "sales",
        "marketing",
        "finance",
    }
    assert plan.corpus_status == "shadow"
    assert plan.admission_verdict == "pending"
    assert plan.AUTHORIZES_EFFECTS is False
    for item in plan.cards:
        assert item.candidate.status is CardStatus.CANDIDATE
        assert item.candidate.retrieval_eligible is False
        assert item.validated.status is CardStatus.VALIDATED
        assert item.validated.retrieval_eligible is True
        assert item.validated.card_version == 2
        assert "expression_originality_checked" in item.lifecycle_reason
        assert "pending_o11_and_fazal_thresholds" in item.lifecycle_reason


def test_originality_gate_rejects_attestation_only_candidate(artifacts):
    rights, candidates = artifacts
    altered = copy.deepcopy(candidates)
    candidate = next(row for row in altered if row["legacy_id"] in SEED_LEGACY_IDS)
    candidate["expression_originality"] = {
        "mode": "attested",
        "scanner": None,
        "attested_by": "synthetic-negative-control",
    }
    candidate["pipeline_steps"][5] = "expression_originality_attested"

    with pytest.raises(RegistrySeedError, match="pipeline evidence|mechanically checked"):
        build_seed_plan(rights, altered)


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = ()) -> "_RecordingConnection":
        self.calls.append((query, params))
        return self


def test_persistence_writes_candidate_then_immutable_validated_version_and_lifecycle(artifacts):
    plan = build_seed_plan(*artifacts)
    conn = _RecordingConnection()
    persist_seed_plan(conn, plan)

    sql = "\n".join(query for query, _ in conn.calls)
    assert sql.count("INSERT INTO public.knowledge_cards") == 30
    assert sql.count("INSERT INTO public.knowledge_lifecycle_events") == 15
    assert sql.count("INSERT INTO public.knowledge_corpus_members") == 15
    assert "admission_verdict" in sql
    assert "'shadow', 'pending'" in sql
    assert "UPDATE public.knowledge_cards" not in sql
    assert "tenant_id" not in sql


class _RowsConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.query = ""

    def execute(self, query: str, params: object = ()) -> "_RowsConnection":
        self.query = query
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def test_loader_round_trips_knowledge_card_from_one_table_without_join(artifacts):
    plan = build_seed_plan(*artifacts)
    card = plan.cards[0].validated
    row = {
        "id": card.card_version_id,
        "card_key": card.card_id,
        "version": card.card_version,
        "corpus_version_id": card.corpus_version_id,
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
    conn = _RowsConnection([row])

    loaded = load_validated_cards(conn, plan.corpus_version_id)

    assert loaded == (card,)
    assert "FROM public.knowledge_cards" in conn.query
    assert " JOIN " not in conn.query.upper()
