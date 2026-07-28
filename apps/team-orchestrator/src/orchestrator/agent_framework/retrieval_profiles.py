"""O8 retrieval profiles declared alongside ACF manifests.

These values are configuration contracts only.  No broker reads them until a later, separately
authorized activation, so adding them cannot inject knowledge into a live prompt.
"""

from __future__ import annotations

from orchestrator.knowledge_contracts import (
    GroundingBehavior,
    KnowledgeAssignmentScope,
    KnowledgeDomain,
    KnowledgeLayer,
    NoResultBehavior,
    RetrievalDepth,
    RetrievalProfile,
    RetrievalStage,
    specialist_assignment_scope,
)


MANAGER_RETRIEVAL_PROFILE = RetrievalProfile(
    identity="team_manager",
    # The Manager is the tenant's COO and knowledge holder: it may synthesize conclusions across
    # every business domain. Specialists remain lane-scoped by identity below.
    domains=frozenset(KnowledgeDomain),
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
    assignment_scopes=frozenset(
        {
            KnowledgeAssignmentScope.MANAGER_GLOBAL.value,
            KnowledgeAssignmentScope.MANAGER_TENANT.value,
        }
    ),
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
        # Narrow by construction: budget does not provide isolation. A specialist sees only
        # cards/customisation explicitly assigned to this exact agent identity.
        assignment_scopes=frozenset({specialist_assignment_scope(identity)}),
    )


MANAGER_RETRIEVAL_PROFILE.validate()


__all__ = ["MANAGER_RETRIEVAL_PROFILE", "specialist_retrieval_profile"]
