"""VT-725 — the retrieval floor is a MEASURED number, and this keeps it honest.

`minimum_score` was fitted against one specific scoring function on one specific corpus. Nothing in
the type system couples the two: someone can retune a weight, or swap Jaccard for something better,
and the floor keeps its old value while the score scale moves underneath it. That is exactly how
0.62 came to sit 2.2x above the best card in the corpus without anyone noticing.

So the pairing is asserted here. These tests do not check that the floor is GOOD — that is what
`canaries/floor_calibration/` measures. They check that the floor and the scale it was measured on
cannot drift apart in silence.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.agent_framework.retrieval_profiles import (  # noqa: E402
    MANAGER_RETRIEVAL_PROFILE,
    MEASURED_RETRIEVAL_FLOOR,
    SPECIALIST_RETRIEVAL_PROFILES,
)
from orchestrator.knowledge.card_retrieval import SCORE_WEIGHTS  # noqa: E402

#: The exact weight vector the 2026-08-07 calibration ran against. NOT a duplicate of the source
#: constant for its own sake — it is the record of what was measured, so a change to the engine
#: fails here instead of quietly invalidating the floor.
CALIBRATED_WEIGHTS = {
    "semantic": 0.38,
    "lexical": 0.24,
    "entity": 0.10,
    "authority": 0.12,
    "applicability": 0.08,
    "confidence": 0.05,
    "recency": 0.03,
}

_REDERIVE = (
    "The retrieval floor was measured against a specific scoring scale. Changing this without "
    "re-deriving the floor leaves a number fitted to a scale that no longer exists — which is how "
    "the old 0.62 ended up above every attainable score. Re-run "
    "apps/team-orchestrator/canaries/floor_calibration/ and update MEASURED_RETRIEVAL_FLOOR."
)


def test_the_weight_vector_the_floor_was_calibrated_against_has_not_moved() -> None:
    assert SCORE_WEIGHTS == CALIBRATED_WEIGHTS, _REDERIVE


def test_weights_still_sum_to_one_so_a_fully_applicable_card_is_unrenormalized() -> None:
    # Renormalization must be a no-op when every dimension applies; otherwise the floor means one
    # thing for evergreen cards and another for dated ones.
    assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def test_every_profile_uses_the_measured_floor_rather_than_its_own_guess() -> None:
    assert MANAGER_RETRIEVAL_PROFILE.minimum_score == MEASURED_RETRIEVAL_FLOOR, _REDERIVE
    for identity, profile in SPECIALIST_RETRIEVAL_PROFILES.items():
        assert profile.minimum_score == MEASURED_RETRIEVAL_FLOOR, f"{identity}: {_REDERIVE}"


def test_the_floor_sits_inside_the_range_scores_can_actually_reach() -> None:
    """The regression that mattered: a floor above the corpus's attainable maximum retrieves
    nothing, forever, while looking like an ordinary tuning constant.

    0.2867 is the highest score observed across all 600 (case, card) pairs in the calibration run.
    A floor at or above it means the corpus is inert. The assertion is deliberately loose — it is a
    sanity rail against the *class* of bug, not a re-assertion of the fitted number.
    """

    highest_observed_card_score = 0.2867
    assert MEASURED_RETRIEVAL_FLOOR < highest_observed_card_score, (
        f"floor {MEASURED_RETRIEVAL_FLOOR} is at or above the best score any card achieved "
        f"({highest_observed_card_score}) — retrieval would return nothing on every turn. " + _REDERIVE
    )
    assert MEASURED_RETRIEVAL_FLOOR > 0.0, "a zero floor is not a floor"
