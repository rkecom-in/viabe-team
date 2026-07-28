"""O8 retrieval profiles declared alongside ACF manifests.

These values are configuration contracts only.  No broker reads them until a later, separately
authorized activation, so adding them cannot inject knowledge into a live prompt.
"""

from __future__ import annotations

from orchestrator.knowledge_contracts import (
    GroundingBehavior,
    KnowledgeDomain,
    KnowledgeLayer,
    NoResultBehavior,
    RetrievalDepth,
    RetrievalProfile,
    RetrievalStage,
)


MANAGER_RETRIEVAL_PROFILE = RetrievalProfile(
    identity="team_manager",
    domains=frozenset({KnowledgeDomain.MANAGEMENT, KnowledgeDomain.CROSS_FUNCTIONAL}),
    layers=frozenset(
        {
            KnowledgeLayer.L1,
            KnowledgeLayer.L2,
            KnowledgeLayer.L3,
            KnowledgeLayer.CONVERSATION,
            KnowledgeLayer.CORRECTION,
            KnowledgeLayer.TASK,
        }
    ),
    stages=frozenset(
        {RetrievalStage.TRIAGE, RetrievalStage.PLANNING, RetrievalStage.REVIEW, RetrievalStage.VERIFICATION}
    ),
    top_k=8,
    token_budget=3_000,
    allow_disputed=True,
    depth=RetrievalDepth.CONCLUSIONS,
    grounding_behavior=GroundingBehavior.REQUIRED,
    no_result_behavior=NoResultBehavior.HEDGE,
    minimum_score=0.62,
)


def specialist_retrieval_profile(
    *,
    identity: str,
    domains: frozenset[KnowledgeDomain],
    top_k: int,
    token_budget: int,
    no_result_behavior: NoResultBehavior = NoResultBehavior.HEDGE,
    allow_disputed: bool = True,
) -> RetrievalProfile:
    """Construct the bounded deep-domain profile a specialist manifest must declare."""

    return RetrievalProfile(
        identity=identity,
        domains=domains,
        layers=frozenset(
            {
                KnowledgeLayer.L1,
                KnowledgeLayer.L2,
                KnowledgeLayer.L3,
                KnowledgeLayer.L4,
                KnowledgeLayer.CORRECTION,
                KnowledgeLayer.TASK,
            }
        ),
        stages=frozenset(
            {RetrievalStage.PLANNING, RetrievalStage.SPECIALIST, RetrievalStage.VERIFICATION}
        ),
        top_k=top_k,
        token_budget=token_budget,
        allow_disputed=allow_disputed,
        depth=RetrievalDepth.DOMAIN_DEEP,
        grounding_behavior=GroundingBehavior.REQUIRED,
        no_result_behavior=no_result_behavior,
        minimum_score=0.58,
    )


MANAGER_RETRIEVAL_PROFILE.validate()


__all__ = ["MANAGER_RETRIEVAL_PROFILE", "specialist_retrieval_profile"]
