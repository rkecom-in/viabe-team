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
    assert result is None


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
    assert result is None, "must not hand the stale plan object to the composer"
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
