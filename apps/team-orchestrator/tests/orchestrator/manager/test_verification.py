"""VT-606 (team-lead ruling round 2) — the completion-verification checkpoint's PURE pieces:
the deterministic floor + the terminal_outcome proxy + the opus structured-extraction call
(mocked client, no network). The DB-backed end-to-end (verify_completion against a real task/steps
+ the workflow's own wiring) is in test_workflow.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("anthropic")

from orchestrator.manager.verification import (  # noqa: E402
    CompletionVerification,
    deterministic_floor_ok,
    resolve_terminal_outcome,
)


def _step(*, status="done", evidence_kind=None, acceptance_criteria=None, step_seq=1):
    return {
        "id": f"step-{step_seq}",  # task_store.get_steps' real column name (VT-633 F-3 run_id derivation)
        "step_seq": step_seq,
        "status": status,
        "evidence_kind": evidence_kind,
        "detail": {"acceptance_criteria": acceptance_criteria or []},
    }


# --- deterministic floor ------------------------------------------------------------------------


def test_floor_ok_when_no_steps_declare_criteria() -> None:
    ok, reason = deterministic_floor_ok([_step(evidence_kind=None, acceptance_criteria=[])])
    assert ok is True
    assert reason == ""


def test_floor_ok_when_criteria_step_has_evidence() -> None:
    ok, _ = deterministic_floor_ok([_step(evidence_kind="pipeline_run", acceptance_criteria=["3+ recovered"])])
    assert ok is True


def test_floor_fails_when_criteria_step_has_no_evidence() -> None:
    ok, reason = deterministic_floor_ok([_step(evidence_kind=None, acceptance_criteria=["3+ recovered"])])
    assert ok is False
    assert "step_seq=1" in reason


def test_floor_ignores_non_done_steps() -> None:
    """A pending/failed step with declared-but-unmet criteria isn't THIS check's concern — only
    steps that reached 'done' claim to have satisfied their criteria."""
    ok, _ = deterministic_floor_ok(
        [_step(status="pending", evidence_kind=None, acceptance_criteria=["x"])]
    )
    assert ok is True


def test_floor_checks_every_done_step_not_just_the_first() -> None:
    ok, reason = deterministic_floor_ok([
        _step(step_seq=1, evidence_kind="pipeline_run", acceptance_criteria=["a"]),
        _step(step_seq=2, evidence_kind=None, acceptance_criteria=["b"]),
    ])
    assert ok is False
    assert "step_seq=2" in reason


# --- terminal_outcome proxy ----------------------------------------------------------------------

_TENANT_ID = "11111111-1111-1111-1111-111111111111"
_TASK_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def _no_campaign_downgrade(monkeypatch: pytest.MonkeyPatch):
    """These tests exercise the evidence-presence proxy itself, not the VT-633 F-3 downgrade check
    (that has its OWN dedicated tests below) — mock the downgrade's own DB read to "nothing to
    downgrade" so the proxy result passes through unchanged."""
    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(
        CampaignsWrapper, "unexecuted_campaign_exists_for_runs", lambda self, tenant_id, run_ids: False
    )


def test_resolve_terminal_outcome_with_effect(_no_campaign_downgrade) -> None:
    steps = [_step(evidence_kind="campaign_plan", step_seq=1)]
    assert resolve_terminal_outcome(_TENANT_ID, _TASK_ID, steps) == "completed_with_effect"


def test_resolve_terminal_outcome_no_action() -> None:
    steps = [_step(evidence_kind=None), _step(evidence_kind=None, step_seq=2)]
    assert resolve_terminal_outcome(_TENANT_ID, _TASK_ID, steps) == "completed_no_action"


def test_resolve_terminal_outcome_any_one_step_with_evidence_is_enough(_no_campaign_downgrade) -> None:
    steps = [_step(step_seq=1, evidence_kind=None), _step(step_seq=2, evidence_kind="pipeline_step")]
    assert resolve_terminal_outcome(_TENANT_ID, _TASK_ID, steps) == "completed_with_effect"


# --- VT-633 F-3: the deterministic executed-effect floor -------------------------------------------


def test_resolve_terminal_outcome_downgrades_unexecuted_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    """A campaign_plan proposal's own evidence_kind satisfies the proxy, but the campaign it
    proposed never actually executed (still 'proposed'/'approved', zero real sends) — the floor
    must downgrade the verdict, never let the proposal-time evidence alone claim an effect."""
    from orchestrator.db.wrappers import CampaignsWrapper

    monkeypatch.setattr(
        CampaignsWrapper, "unexecuted_campaign_exists_for_runs", lambda self, tenant_id, run_ids: True
    )
    steps = [_step(evidence_kind="campaign_plan", step_seq=1)]

    assert resolve_terminal_outcome(_TENANT_ID, _TASK_ID, steps) == "completed_no_action"


def test_resolve_terminal_outcome_keeps_proxy_result_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-soft: a DB error during the downgrade check must never crash the loop, and must never
    itself change what the proxy already decided."""
    from orchestrator.db.wrappers import CampaignsWrapper

    def _boom(self, tenant_id, run_ids):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(CampaignsWrapper, "unexecuted_campaign_exists_for_runs", _boom)
    steps = [_step(evidence_kind="campaign_plan", step_seq=1)]

    assert resolve_terminal_outcome(_TENANT_ID, _TASK_ID, steps) == "completed_with_effect"


# --- the structured verdict model -----------------------------------------------------------------


def test_completion_verification_model_accepts_verified() -> None:
    cv = CompletionVerification(verdict="verified", reason="all criteria evidenced")
    assert cv.verdict == "verified"


def test_completion_verification_model_rejects_unknown_verdict() -> None:
    with pytest.raises(Exception):
        CompletionVerification(verdict="maybe")  # type: ignore[arg-type]
