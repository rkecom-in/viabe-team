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


#: VT-725 — the retrieval floor, DERIVED not chosen (Fazal "Recalibrate.", 2026-08-07).
#:
#: The previous 0.62 was a guess, and a structurally unreachable one: measured against the real
#: 100-card dev corpus with real Voyage embeddings, the single highest-scoring card across 600
#: (case, card) pairs scored **0.2867**.  No card could ever clear 0.62, so the corpus was inert by
#: arithmetic rather than by any judgement about the knowledge in it.
#:
#: How this number was obtained (re-derivable — see `canaries/floor_calibration/README.md`):
#:   1. Every card in all 6 O11 cases was labelled relevant/irrelevant BLIND — the labeller saw the
#:      agent view only (never the answer key), never a score or rank, with the card order shuffled
#:      per pass.  3 independent passes, majority vote.
#:   2. The scorer separates the two classes: pooled dev AUC = **0.753** (chance = 0.5).
#:   3. Floor swept on the DEVELOPMENT split only; VALIDATION was held out and never used to pick.
#:      Precision is measured on cards actually INJECTED (floor, then top_k), not on all scored.
#:
#:        floor | dev precision | val precision | cases still retrieving
#:        0.000 |     0.375     |     0.174     | 3/3 dev, 3/3 val   <- no floor
#:        0.245 |     0.471     |       -       | 3/3 dev
#:        0.250 |   **0.533**   | **0.500-0.600**| 3/3 dev, 2/3-3/3 val   <- CHOSEN
#:        0.255 |     0.538     |     0.500     | 3/3 dev, 2/3 val
#:        0.265 |     0.800     |       -       | 2/3 dev  (n=5 — not a real number)
#:        0.290 |      n/a      |      n/a      | 0/3 — nothing retrieves at all
#:
#: 0.250 is the measured knee on dev (precision 0.471 -> 0.533) with every dev case still retrieving.
#: The mandate was to bias the margin toward precision; going higher buys precision only on samples
#: of n<=5 while silently zeroing whole cases, which is the failure mode the bias exists to prevent.
#: On data it was never fitted to it takes precision from 0.174 to 0.500-0.600.
#:
#: The val range is a RANGE because one held-out case sits exactly on the floor: the best card in
#: `val-restaurant-festival-capacity` scores 0.2505 / 0.2500 on repeat runs (Voyage embeddings are
#: not deterministic, +/-0.0019 on semantic). That card is labelled IRRELEVANT, so val precision
#: RISES when it drops out — the flap costs a false positive, not a real retrieval.
#:
#: KNOWN LIMITS, stated so nobody reads this as better than it is:
#:   * Recall at 0.250 is 0.229 — most relevant cards still never surface.  That is the RANKING, not
#:     the floor; the floor cannot fix it and was not asked to.
#:   * 9 of 35 dev relevant cards are hard-excluded by applicability BEFORE scoring, so the floor
#:     never sees them.
#:   * The floor is calibrated to `card_retrieval.SCORE_WEIGHTS` and its metrics.  Change a weight
#:     or a component metric and this number must be re-derived (a test enforces the pairing).
MEASURED_RETRIEVAL_FLOOR = 0.250


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
    minimum_score=MEASURED_RETRIEVAL_FLOOR,
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
        # Specialists INHERIT the Manager's measured floor: 0.58 was the same unreachable guess as
        # 0.62 and would keep every specialist retrieving nothing. There is no labelled specialist
        # set yet, so this number is measured for the Manager and BORROWED here — flagged as a gap
        # rather than presented as its own measurement, and to be re-derived per lane when a
        # specialist-labelled set exists.
        minimum_score=MEASURED_RETRIEVAL_FLOOR,
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
