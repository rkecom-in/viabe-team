"""VT-680 (§7C) — the ONLINE impact judge's workflow-level wiring (``_judge_impact_step`` +
``_run_verification_cycle``'s threading of its verdict into the terminal notify).

PURE / no DB, no DBOS launch: ``manager/workflow.py`` imports cleanly without a live Postgres
connection (its ``@DBOS.step()`` decorators only register metadata; nothing connects until a step
actually runs), and ``_judge_impact_step`` / ``_run_verification_cycle`` are plain-Python functions
callable directly — mirrors ``test_vt611_state_machine_matrix.py``'s PURE tests (e.g.
``test_run_verification_cycle_threads_campaign_reported_into_settle``), which already prove this
module is directly testable this way. The DB-backed end-to-end (a real settle + a real completed
task carrying a real impact_judged tm_audit row) is covered by the live-Postgres suites
(``test_workflow.py`` / ``test_vt611_state_machine_matrix.py``) plus the dev-drive gate — this file
covers only the flag gate, the fail-soft boundary, and the verdict-threading wiring.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")

import orchestrator.manager.workflow as wf  # noqa: E402
from orchestrator.manager import verification  # noqa: E402


# --------------------------- _judge_impact_step: the flag gate + fail-soft boundary ---------------


def test_judge_impact_step_flag_off_never_calls_the_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag unset) must be OFF, and OFF must not even invoke ``judge_impact`` — the
    settle path stays byte-identical to pre-VT-680 with zero extra LLM calls."""
    monkeypatch.delenv("TEAM_IMPACT_JUDGE", raising=False)

    def _must_not_be_called(*a, **k):
        raise AssertionError("judge_impact must not be called when TEAM_IMPACT_JUDGE is off")

    monkeypatch.setattr(verification, "judge_impact", _must_not_be_called)

    verdict, reason = wf._judge_impact_step("t", "k")

    assert verdict == "unjudged"
    assert reason == "flag_off"


def test_judge_impact_step_flag_on_success_emits_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_IMPACT_JUDGE", "1")
    monkeypatch.setattr(
        verification, "judge_impact",
        lambda tenant_id, task_id: verification.ImpactVerdict(verdict="partial", reason="1 of 3"),
    )
    audits: list[dict] = []
    monkeypatch.setattr(
        wf, "emit_tm_audit",
        lambda **kwargs: audits.append(kwargs) or None,
    )

    verdict, reason = wf._judge_impact_step("tenant-1", "task-1")

    assert verdict == "partial"
    assert reason == "1 of 3"
    assert len(audits) == 1
    assert audits[0]["event_layer"] == "decides"
    assert audits[0]["event_kind"] == "impact_judged"
    assert audits[0]["actor"] == "team_manager"
    assert audits[0]["decision"] == {"verdict": "partial", "reason": "1 of 3"}
    assert audits[0]["reasoning_ref"] == {"run_id": "task-1", "step_name": "impact_judge"}
    assert audits[0]["conn"] is None


def test_judge_impact_step_flag_on_judge_failure_fails_soft_to_unjudged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge NEVER blocks/retries the settle: any raised exception (network, parse, schema —
    ``judge_impact`` itself does not fail-soft internally, per its own docstring) is caught HERE and
    reported as 'unjudged', not propagated."""
    monkeypatch.setenv("TEAM_IMPACT_JUDGE", "1")

    def _boom(tenant_id, task_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(verification, "judge_impact", _boom)
    audits: list[dict] = []
    monkeypatch.setattr(wf, "emit_tm_audit", lambda **kwargs: audits.append(kwargs))

    verdict, reason = wf._judge_impact_step("t", "k")

    assert verdict == "unjudged"
    assert "impact_judge_failed" in reason
    assert "RuntimeError" in reason
    assert audits == []  # no audit row for a failed judge — nothing to record


def test_judge_impact_step_flag_off_variants_are_all_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the exact literal "1" activates the flag (mirrors ``auth.prod_safety._flag_on``'s own
    activation contract) — "true"/"on"/"yes" are all still OFF, never a silent surprise-ON."""
    monkeypatch.setattr(
        verification, "judge_impact",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    for value in ("true", "on", "yes", "0", ""):
        monkeypatch.setenv("TEAM_IMPACT_JUDGE", value)
        verdict, reason = wf._judge_impact_step("t", "k")
        assert verdict == "unjudged"
        assert reason == "flag_off"


# --------------------------- _run_verification_cycle: threading into the notify -------------------


def test_run_verification_cycle_threads_impact_verdict_into_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wf, "_verify_completion_step", lambda t, k: ("verified", "all good"))
    monkeypatch.setattr(wf, "_settle_verified_task", lambda *a, **k: None)
    monkeypatch.setattr(wf, "_judge_impact_step", lambda t, k: ("partial", "fell short"))
    notified: dict = {}
    monkeypatch.setattr(
        wf, "_notify_owner_of_terminal",
        lambda t, k, **kw: notified.update(kw),
    )

    action, attempts = wf._run_verification_cycle("t", "k", 0)

    assert action == "settled"
    assert attempts == 0
    assert notified == {"impact_verdict": "partial"}


def test_run_verification_cycle_flag_off_notify_gets_unjudged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real (non-mocked) ``_judge_impact_step`` with the flag off — end-to-end proof that the
    'flag off' default threads 'unjudged' through, which the composer treats as a no-op note."""
    monkeypatch.delenv("TEAM_IMPACT_JUDGE", raising=False)
    monkeypatch.setattr(wf, "_verify_completion_step", lambda t, k: ("verified", "all good"))
    monkeypatch.setattr(wf, "_settle_verified_task", lambda *a, **k: None)
    notified: dict = {}
    monkeypatch.setattr(wf, "_notify_owner_of_terminal", lambda t, k, **kw: notified.update(kw))

    action, _attempts = wf._run_verification_cycle("t", "k", 0)

    assert action == "settled"
    assert notified == {"impact_verdict": "unjudged"}


def test_run_verification_cycle_not_verified_never_calls_the_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge only ever runs on the 'verified' branch — a not_verified/retry cycle must not
    touch it at all."""
    monkeypatch.setattr(wf, "_verify_completion_step", lambda t, k: ("not_verified", "gap"))
    monkeypatch.setattr(wf, "_append_verification_retry_step", lambda t, k, *, reason: True)

    def _must_not_be_called(t, k):
        raise AssertionError("judge must not run on a not_verified cycle")

    monkeypatch.setattr(wf, "_judge_impact_step", _must_not_be_called)

    action, attempts = wf._run_verification_cycle("t", "k", 0)

    assert action == "retry"
    assert attempts == 1
