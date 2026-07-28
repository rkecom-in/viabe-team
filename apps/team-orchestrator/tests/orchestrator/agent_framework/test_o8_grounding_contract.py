"""VT-709 — provenance survives every additive framework/Manager adapter."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("langchain")

from orchestrator.agent_framework.capabilities import AgentRole  # noqa: E402
from orchestrator.agent_framework.context import ModuleResult  # noqa: E402
from orchestrator.knowledge_contracts import (  # noqa: E402
    ConflictManifestEntry,
    EvidenceManifestEntry,
    GroundingStatus,
    KnowledgeVersion,
    grounding_audit_payload,
)
from orchestrator.manager.plan_models import PlanSpecialistReturn  # noqa: E402
from orchestrator.manager.review import (  # noqa: E402
    preserve_module_grounding,
    to_legacy_specialist_return,
)


def grounded_result() -> ModuleResult:
    evidence = (
        EvidenceManifestEntry(
            evidence_id="evidence-1",
            source_id="source-1",
            claim_key="subject|predicate|in|smb|whatsapp",
            authority="verified_system",
            confidence="high",
            independence_cluster="cluster-1",
            card_version_id="card-1:v2",
            corpus_version_id="corpus-7",
        ),
        EvidenceManifestEntry(
            evidence_id="evidence-2",
            source_id="source-2",
            claim_key="subject|predicate|in|smb|whatsapp",
            authority="vtr",
            confidence="medium",
            independence_cluster="cluster-2",
            card_version_id="card-2:v1",
            corpus_version_id="corpus-7",
        ),
    )
    return ModuleResult(
        role=AgentRole.PROPOSER,
        status="completed",
        proposal={"recommendation": "hedged because the evidence conflicts"},
        evidence_manifest=evidence,
        conflict_manifest=(
            ConflictManifestEntry(
                claim_key="subject|predicate|in|smb|whatsapp",
                evidence_ids=("evidence-1", "evidence-2"),
            ),
        ),
        knowledge_version=KnowledgeVersion(corpus_version_id="corpus-7"),
        grounding_status=GroundingStatus.DISPUTED,
    )


def test_module_result_preserves_grounding_through_both_framework_adapters() -> None:
    result = grounded_result()
    agent_result = result.to_agent_result()
    assert agent_result.evidence_manifest == result.evidence_manifest
    assert agent_result.conflict_manifest == result.conflict_manifest
    assert agent_result.knowledge_version == result.knowledge_version
    assert agent_result.grounding_status is GroundingStatus.DISPUTED

    executor_result = ModuleResult(
        role=AgentRole.EXECUTOR,
        status="completed",
        evidence_manifest=result.evidence_manifest,
        conflict_manifest=result.conflict_manifest,
        knowledge_version=result.knowledge_version,
        grounding_status=result.grounding_status,
    ).to_item_execution_result()
    assert executor_result.evidence_manifest == result.evidence_manifest
    assert executor_result.conflict_manifest == result.conflict_manifest


def test_grounding_survives_specialist_return_and_manager_synthesis() -> None:
    result = grounded_result()
    specialist = preserve_module_grounding(
        PlanSpecialistReturn(
            status="completed",
            action_summary="prepared a recommendation",
            outcome_summary="a disputed recommendation was returned with a hedge",
        ),
        result,
    )
    legacy = to_legacy_specialist_return(specialist)
    assert legacy.evidence_manifest == result.evidence_manifest
    assert legacy.conflict_manifest == result.conflict_manifest
    assert legacy.knowledge_version == result.knowledge_version
    assert legacy.grounding_status is GroundingStatus.DISPUTED

    audit = grounding_audit_payload(
        evidence_manifest=legacy.evidence_manifest,
        conflict_manifest=legacy.conflict_manifest,
        knowledge_version=legacy.knowledge_version,
        grounding_status=legacy.grounding_status,
    )
    assert audit["knowledge_version"]["corpus_version_id"] == "corpus-7"
    assert audit["grounding_status"] == "disputed"
    serialized = repr(audit).lower()
    assert "recommendation" not in serialized
    assert "tenant" not in serialized


def test_grounding_invariants_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires evidence"):
        ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            grounding_status=GroundingStatus.GROUNDED,
        )
