"""VT-241 — unit coverage for ``dispatch._classify_terminal`` cohort branch.

Pure-function tests (no DB, no LLM): assert the fail-closed cohort rejection
classifies as a CLEAN ``completed`` terminal on the ``collapse`` path, carries
a count-only reason discriminator, and is ordered correctly against the other
terminal markers (escalation wins; the rejection wins over a still-in-state
``campaign_plan`` object because nothing actually persisted).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# dispatch imports the langchain/langgraph stack at module load.
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")


def test_classify_terminal_cohort_rejected_is_clean_completed():
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, result = _classify_terminal(
        {"campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 3}}
    )

    assert path == "collapse"
    # fail-closed is a VALID terminal — reuses 'completed' (no new
    # pipeline_runs.status value → no CHECK-constraint migration).
    assert status == "completed"
    # count-only discriminator — never the rejected ids.
    assert reason == "campaign_not_sent_invalid_cohort:3"
    # VT-248: the carrier hands ONLY the count to the composer (the
    # team_campaign_not_sent {{2}} param) — never ids, never the plan object.
    assert result is not None
    assert result.output == {"rejected_count": 3}


def test_classify_terminal_cohort_reject_wins_over_stale_plan():
    """The collapse rollback leaves the ``campaign_plan`` object in state even
    though no campaign row persisted. The rejection MUST be checked first, or
    a rejected campaign would be misclassified as a successful collapse."""
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, result = _classify_terminal(
        {
            "campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 1},
            "campaign_plan": SimpleNamespace(status="proposed"),
        }
    )

    assert reason == "campaign_not_sent_invalid_cohort:1"
    # VT-248: the composer receives the count-only carrier, NOT the stale plan.
    # The plan is a SimpleNamespace(status=...); the carrier has no `status`.
    assert result.output == {"rejected_count": 1}
    assert not hasattr(result, "status"), (
        "must not hand the stale plan object to the composer"
    )
    assert (path, status) == ("collapse", "completed")


def test_classify_terminal_escalation_wins_over_cohort_reject():
    """Escalation is the highest-priority terminal marker — an escalated run
    that also carried a rejected campaign still classifies as escalated."""
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, _ = _classify_terminal(
        {
            "messages": [
                SimpleNamespace(name="escalate_to_fazal", content="needs human")
            ],
            "campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 2},
        }
    )

    assert (path, status) == ("escalated", "escalated")
    assert reason == "needs human"


def test_classify_terminal_missing_count_defaults_to_zero():
    """Defensive: a malformed rejection dict without rejected_count still
    classifies cleanly (count defaults to 0) rather than KeyError-ing the run."""
    from orchestrator.agent.dispatch import _classify_terminal

    _, status, reason, _ = _classify_terminal({"campaign_rejected": {}})

    assert status == "completed"
    assert reason == "campaign_not_sent_invalid_cohort:0"


def test_classify_terminal_threads_count_to_composer_carrier():
    """VT-248: the cohort-rejected carrier exposes ``.output['rejected_count']``
    — the channel the composer reads to fill the team_campaign_not_sent {{2}}
    count. Count only; the carrier never holds ids."""
    from orchestrator.agent.dispatch import _CohortRejectedResult, _classify_terminal

    _, _, _, result = _classify_terminal(
        {"campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 9}}
    )
    assert isinstance(result, _CohortRejectedResult)
    assert result.rejected_count == 9
    assert result.output == {"rejected_count": 9}


# --- VT-47: 'paused' terminal --------------------------------------------------


def test_paused_is_a_valid_final_status():
    """The new 'paused' terminal is part of the FinalStatus literal so it can
    flow to pipeline_runs.status (migration 052 CHECK)."""
    from typing import get_args

    from orchestrator.agent.dispatch import FinalStatus

    assert "paused" in get_args(FinalStatus)


def test_interrupt_state_is_handled_before_classify_terminal():
    """Contract: dispatch_brain detects ``__interrupt__`` in the returned
    state (langgraph surfaces it there — it does NOT raise) and maps it to
    'paused' BEFORE calling _classify_terminal. _classify_terminal itself has
    no paused branch by design; an interrupted state never reaches it. This
    test pins the surface key langgraph uses so a version bump that renames it
    is caught here rather than silently turning every pause into 'completed'."""
    interrupted_state = {"foo": "x", "__interrupt__": ["sentinel"]}
    assert interrupted_state.get("__interrupt__"), (
        "dispatch_brain keys the paused branch on '__interrupt__' truthiness"
    )


# --- VT-480: brain model tiering (select_brain_model) --------------------------
#
# Fazal CHOSE tiering over raising the ₹5 cost cap: route ROUTINE turns to Sonnet
# (cheap, completes within the cap), reserve Opus for COMPLEX/ambiguous reasoning.
# These are pure-function tests — no LLM, no DB — and assert the selection reuses
# the ALREADY-COMPUTED intent (just a dict) and fails safe to Opus.


def test_select_brain_model_routine_intent_picks_sonnet():
    """A clearly-simple intent (a one-step approval ack) → Sonnet — the cheap
    path that completes within the ₹5 cap."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_SONNET,
        select_brain_model,
    )

    model_id, tier = select_brain_model({"classification": "approval", "confidence": 0.95})

    assert model_id == _BRAIN_MODEL_SONNET == "claude-sonnet-4-6"
    assert tier == "sonnet"


@pytest.mark.parametrize(
    "routine", ["approval", "rejection", "question", "status_query"]
)
def test_select_brain_model_all_routine_intents_pick_sonnet(routine: str):
    """Every intent in the routine allow-set routes to Sonnet."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": routine})
    assert (model_id, tier) == ("claude-sonnet-4-6", "sonnet")


def test_select_brain_model_business_action_picks_opus():
    """A business action / send request (adhoc_campaign_request → owner_initiated)
    is COMPLEX → Opus. Under-powering a customer-facing decision is worse than
    the cost."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_OPUS,
        select_brain_model,
    )

    model_id, tier = select_brain_model(
        {"classification": "adhoc_campaign_request", "confidence": 0.9}
    )

    assert model_id == _BRAIN_MODEL_OPUS == "claude-opus-4-8"
    assert tier == "opus"


@pytest.mark.parametrize(
    "complex_intent",
    ["feedback", "first_data_step_onboarding", "exclusion_request", "other"],
)
def test_select_brain_model_complex_or_ambiguous_picks_opus(complex_intent: str):
    """Anything NOT in the routine allow-set — business signals, onboarding
    spawns, ambiguous mutations, the 'other' catch-all — stays on Opus."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": complex_intent})
    assert (model_id, tier) == ("claude-opus-4-8", "opus")


def test_select_brain_model_missing_signal_fails_safe_to_opus():
    """CORRECTNESS-FIRST fail-safe: an empty intent dict (classify skipped or
    failed) → Opus, the capable model. Never under-power a turn we couldn't
    read."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({})
    assert (model_id, tier) == ("claude-opus-4-8", "opus")


def test_select_brain_model_unknown_classification_fails_safe_to_opus():
    """A classification string we don't recognise (e.g. a future intent added to
    the classifier before this allow-set is updated) defaults to Opus, not a
    crash."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": "some_new_intent"})
    assert (model_id, tier) == ("claude-opus-4-8", "opus")


def test_select_brain_model_non_string_classification_fails_safe_to_opus():
    """Defensive: a malformed signal (classification is None / not a str) must
    not crash the selector — it fails safe to Opus."""
    from orchestrator.agent.dispatch import select_brain_model

    assert select_brain_model({"classification": None})[1] == "opus"


def test_brain_model_ids_are_the_single_source_of_truth():
    """The two model-id constants are the ONE place the brain model strings
    live; select_brain_model returns exactly those values."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_OPUS,
        _BRAIN_MODEL_SONNET,
        select_brain_model,
    )

    assert _BRAIN_MODEL_SONNET == "claude-sonnet-4-6"
    assert _BRAIN_MODEL_OPUS == "claude-opus-4-8"
    assert select_brain_model({"classification": "question"})[0] == _BRAIN_MODEL_SONNET
    assert select_brain_model({})[0] == _BRAIN_MODEL_OPUS


def test_brain_models_are_in_the_cost_rate_table():
    """VT-480 invariant: both tiered brain models MUST be in the cost RATES
    table, or the cost callback silently skips attribution (KeyError caught +
    logged) and the ₹5 cost-cap telemetry zeroes out for that run."""
    from orchestrator.agent.cost import RATES
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_OPUS,
        _BRAIN_MODEL_SONNET,
    )

    assert _BRAIN_MODEL_SONNET in RATES
    assert _BRAIN_MODEL_OPUS in RATES
