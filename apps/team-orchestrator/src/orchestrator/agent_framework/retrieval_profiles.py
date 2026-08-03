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
            # L4 = the GLOBAL curated card corpus.  Codex's original profile omitted it because the
            # authored-seed loader was dead scope; CL-2026-07-29-manager-owns-memory then made the
            # Manager the holder of that corpus, and every curated card carries scope='global'
            # (= L4).  Without L4 declared here the Manager's serving pool is structurally empty.
            KnowledgeLayer.L4,
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


#: The declared per-identity retrieval budget for every specialist that can be dispatched today
#: (o8 design §5.6: corpus domains, layers, top-k, token budget are a declared capability).  The
#: domain sets are the LANE, not a convenience: a specialist's serving pool is filtered on them
#: before ranking, so a lane it does not declare is not merely down-ranked — it is never loaded.
SPECIALIST_RETRIEVAL_PROFILES: dict[str, RetrievalProfile] = {
    "onboarding_conductor": specialist_retrieval_profile(
        identity="onboarding_conductor",
        domains=frozenset({KnowledgeDomain.ONBOARDING, KnowledgeDomain.COMPLIANCE}),
        top_k=6,
        token_budget=2_000,
    ),
    "integration_agent": specialist_retrieval_profile(
        identity="integration_agent",
        domains=frozenset({KnowledgeDomain.INTEGRATION, KnowledgeDomain.TECHNOLOGY}),
        top_k=6,
        token_budget=2_000,
    ),
    "sales_recovery_agent": specialist_retrieval_profile(
        identity="sales_recovery_agent",
        domains=frozenset({KnowledgeDomain.SALES, KnowledgeDomain.MARKETING}),
        top_k=8,
        token_budget=3_000,
    ),
}

MANAGER_IDENTITY = MANAGER_RETRIEVAL_PROFILE.identity


def retrieval_profile_for(identity: str) -> RetrievalProfile:
    """Resolve one declared profile, or raise.  An undeclared identity retrieves NOTHING rather
    than silently inheriting the Manager's breadth — budget is a capability, not a default."""

    if identity == MANAGER_IDENTITY:
        return MANAGER_RETRIEVAL_PROFILE
    try:
        return SPECIALIST_RETRIEVAL_PROFILES[identity]
    except KeyError as exc:
        raise KeyError(f"no declared O8 retrieval profile for identity {identity!r}") from exc


MANAGER_RETRIEVAL_PROFILE.validate()
for _profile in SPECIALIST_RETRIEVAL_PROFILES.values():
    _profile.validate()


__all__ = [
    "MANAGER_IDENTITY",
    "MANAGER_RETRIEVAL_PROFILE",
    "SPECIALIST_RETRIEVAL_PROFILES",
    "retrieval_profile_for",
    "specialist_retrieval_profile",
]
