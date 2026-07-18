"""VT-606 (team-lead ruling round 2) — the completion-verification checkpoint's PURE pieces:
the deterministic floor + the terminal_outcome proxy + the opus structured-extraction call
(mocked client, no network). The DB-backed end-to-end (verify_completion against a real task/steps
+ the workflow's own wiring) is in test_workflow.py.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("anthropic")

from orchestrator.manager.verification import (  # noqa: E402
    CompletionVerification,
    ImpactVerdict,
    deterministic_floor_ok,
    judge_impact,
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


def test_upward_floor_verifies_an_executed_campaign_without_llm(monkeypatch):
    """VT-633 #52 — a DB-proven executed campaign VERIFIES deterministically; the opus call must
    never run (client=None would crash if reached — that's the proof)."""
    from uuid import uuid4

    from orchestrator.db.wrappers import CampaignsWrapper
    from orchestrator.manager import task_store, verification

    tid, kid = uuid4(), uuid4()
    monkeypatch.setattr(task_store, "get_task", lambda t, k: {"plan_revision": 1, "objective": {}})
    monkeypatch.setattr(
        verification, "_current_steps",
        lambda t, k, r: [{"id": uuid4(), "step_seq": 1, "status": "done", "evidence_kind": "x", "detail": {}}],
    )
    monkeypatch.setattr(
        CampaignsWrapper, "executed_campaign_exists_for_runs", lambda self, t, r, **k: True
    )
    v = verification.verify_completion(tid, kid, client=None)
    assert v.verdict == "verified"
    assert "executed-effect floor" in v.reason


def test_upward_floor_read_error_falls_through_to_llm(monkeypatch):
    """A floor read error must fall through to the normal checkpoint (fail-soft, never a
    fabricated 'verified'). client=None then fails CLOSED to not_verified — proving the LLM
    path was reached."""
    from uuid import uuid4

    from orchestrator.db.wrappers import CampaignsWrapper
    from orchestrator.manager import task_store, verification

    tid, kid = uuid4(), uuid4()
    monkeypatch.setattr(task_store, "get_task", lambda t, k: {"plan_revision": 1, "objective": {}})
    monkeypatch.setattr(
        verification, "_current_steps",
        lambda t, k, r: [{"id": uuid4(), "step_seq": 1, "status": "done", "evidence_kind": "x", "detail": {}}],
    )
    def _boom(self, t, r, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(CampaignsWrapper, "executed_campaign_exists_for_runs", _boom)
    v = verification.verify_completion(tid, kid, client=None)
    assert v.verdict != "verified" or "executed-effect floor" not in v.reason


def test_no_effect_fast_path_verifies_without_llm(monkeypatch):
    """VT-633 #51 — a completed_no_action terminal (honest empty-cohort conclusion) verifies
    deterministically; the opus call must never run (client=None crashes if reached). Closes the
    chicken-and-egg where the 'owner-visible reply' criterion could only be satisfied by the
    settle-notify that runs AFTER this verdict."""
    from uuid import uuid4

    from orchestrator.manager import task_store, verification

    tid, kid = uuid4(), uuid4()
    monkeypatch.setattr(task_store, "get_task", lambda t, k: {"plan_revision": 1, "objective": {}})
    monkeypatch.setattr(
        verification, "_current_steps",
        lambda t, k, r: [{"id": uuid4(), "step_seq": 1, "status": "done", "evidence_kind": None, "detail": {}}],
    )
    v = verification.verify_completion(tid, kid, client=None)
    assert v.verdict == "verified"
    assert "no-effect fast path" in v.reason


# --- VT-680 (§7C) — the ONLINE impact judge -------------------------------------------------------


def _json_text_call(payload: dict):
    """Mirrors ``structured_text_call``'s signature ``(tier, *, system, user, max_tokens, agent,
    call_site, tenant_id)`` — accepts and ignores whatever the site passes, returns ``payload`` as
    JSON text (matches ``test_plan_validation.py``'s own stub shape)."""

    def _call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        return json.dumps(payload)

    return _call


def _raw_text_call(raw: str):
    def _call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        return raw

    return _call


def _raising_text_call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
    raise RuntimeError("network down")


def test_impact_verdict_model_accepts_all_three_verdicts() -> None:
    for verdict in ("met", "partial", "unmet"):
        iv = ImpactVerdict(verdict=verdict, reason="x")
        assert iv.verdict == verdict


def test_impact_verdict_model_rejects_unknown_verdict() -> None:
    with pytest.raises(Exception):
        ImpactVerdict(verdict="unjudged")  # type: ignore[arg-type]  # NOT a member of this Literal


def test_impact_verdict_model_rejects_extra_fields() -> None:
    with pytest.raises(Exception):
        ImpactVerdict(verdict="met", reason="x", extra_field="nope")  # type: ignore[call-arg]


def _seed_task_and_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.manager import task_store, verification

    monkeypatch.setattr(
        task_store, "get_task",
        lambda t, k: {
            "plan_revision": 1,
            "objective": {"objective": "win back lapsed customers"},
            "acceptance_criteria": {"acceptance_criteria": ["3+ customers recovered within 7 days"]},
        },
    )
    monkeypatch.setattr(
        verification, "_current_steps",
        lambda t, k, r: [
            {
                "step_seq": 1,
                "status": "done",
                "kind": "specialist_dispatch",
                "evidence_kind": "campaign_plan",
                "detail": {
                    "desired_outcome": "recover 3+ lapsed customers",
                    "acceptance_criteria": ["3+ customers recovered"],
                },
            }
        ],
    )


def test_judge_impact_met(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    result = judge_impact(
        uuid4(), uuid4(),
        text_call=_json_text_call({"verdict": "met", "reason": "criteria satisfied"}),
    )
    assert result.verdict == "met"


def test_judge_impact_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    result = judge_impact(
        uuid4(), uuid4(),
        text_call=_json_text_call({"verdict": "partial", "reason": "only 1 of 3 recovered"}),
    )
    assert result.verdict == "partial"
    assert result.reason


def test_judge_impact_raises_on_non_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike ``verify_completion``/``validate_plan_draft``, ``judge_impact`` does NOT fail-soft
    internally — ``ImpactVerdict`` has no 'unjudged' member to degrade to, so a parse failure
    propagates; the caller (``workflow._judge_impact_step``) is the fail-soft boundary."""
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    with pytest.raises(ValueError, match="non-JSON"):
        judge_impact(uuid4(), uuid4(), text_call=_raw_text_call("not json"))


def test_judge_impact_raises_on_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    with pytest.raises(ValueError, match="empty response"):
        judge_impact(uuid4(), uuid4(), text_call=_raw_text_call("   "))


def test_judge_impact_raises_on_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    with pytest.raises(Exception):
        judge_impact(
            uuid4(), uuid4(),
            text_call=_json_text_call({"verdict": "sort-of"}),  # not a member of the Literal
        )


def test_judge_impact_raises_on_client_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    with pytest.raises(RuntimeError, match="network down"):
        judge_impact(uuid4(), uuid4(), text_call=_raising_text_call)


def test_judge_impact_raises_when_task_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    from orchestrator.manager import task_store

    monkeypatch.setattr(task_store, "get_task", lambda t, k: None)
    with pytest.raises(ValueError, match="task not found"):
        judge_impact(uuid4(), uuid4(), text_call=_json_text_call({"verdict": "met"}))


def test_judge_impact_reads_desired_outcome_from_step_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the substrate this reads is exactly what ``verify_completion`` reads PLUS each step's
    OWN ``desired_outcome`` (VT-680 D1) — captured via the ``text_call`` stub's ``user`` kwarg."""
    from uuid import uuid4

    _seed_task_and_steps(monkeypatch)
    captured: dict = {}

    def _capture(tier, *, system, user, max_tokens, agent, call_site, tenant_id):
        captured["user"] = user
        captured["agent"] = agent
        captured["call_site"] = call_site
        return json.dumps({"verdict": "met", "reason": "ok"})

    judge_impact(uuid4(), uuid4(), text_call=_capture)

    assert "recover 3+ lapsed customers" in captured["user"]  # the step's own desired_outcome
    assert captured["agent"] == "team_manager"
    assert captured["call_site"] == "impact_judge"
