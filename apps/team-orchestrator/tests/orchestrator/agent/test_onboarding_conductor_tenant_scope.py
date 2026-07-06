"""VT-603 — onboarding_conductor tools derive tenant from the run context, never the model.

Same defect class as ``integration_agent`` (VT-599 follow-on, CC-verified): both
``next_required_question`` and ``profile_completion_check`` declared ``tenant_id: str`` as a
MODEL-FILLABLE parameter and passed it STRAIGHT to ``UUID(tenant_id)`` before routing into
``onboarding.conductor`` / ``onboarding.journey`` / ``onboarding.draft_profile`` — all of which are
tenant-scoped via ``tenant_connection`` (RLS keyed by whatever tenant they're handed). A
model-authored foreign UUID is the VT-293/294 IDOR class: a cross-tenant onboarding-state READ.

Mirrors ``test_marketing_lane_tenant_scope.py`` / ``test_integration_agent_tenant_scope.py``
(VT-599 / VT-603): every tool now calls ``resolve_lane_tenant`` first — the ambient dispatch
``ObservabilityContext`` is ALWAYS authoritative; a disagreeing model value (a business name, a
foreign UUID) is observed + logged (mismatch WARNING) but never trusted; no context + an
unparseable model value returns the structured ``lane_tenant_error`` dict, never a raise.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")

from orchestrator.observability.decorators import observability_context  # noqa: E402

_LOGGER_NAME = "orchestrator.agent.lane_tenant"


# --- scenario helper (mirrors test_marketing_lane_tenant_scope.py) ----------------------------


def _assert_context_wins_no_raise(
    caplog: pytest.LogCaptureFixture,
    *,
    call: Any,
    tool_name: str,
) -> Any:
    """Runs ``call`` (a zero-arg closure invoking the tool) inside a caplog scope; returns the
    tool's result. Asserts exactly one mismatch warning naming ``tool_name`` was logged."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = call()
    mismatches = [r for r in caplog.records if tool_name in r.getMessage()]
    assert len(mismatches) == 1, caplog.text
    assert "mismatch" in mismatches[0].getMessage().lower()
    return result


# --- (1) next_required_question ----------------------------------------------------------------


def test_next_required_question_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.conductor as conductor_mod
    from orchestrator.agent.onboarding_conductor import next_required_question
    from orchestrator.onboarding.conductor import ConductorDecision
    from orchestrator.onboarding.question_brain import Question

    seen: dict[str, Any] = {}
    q = Question(field="city", kind="confirm", prompt_en="In Pune?", prompt_hi="पुणे में?", draft_value="Pune")

    def _fake_next_question_for_tenant(tid: Any) -> ConductorDecision:
        seen["tenant_id"] = tid
        return ConductorDecision(next_question=q, remaining=(q,), known=(), skipped=())

    monkeypatch.setattr(conductor_mod, "next_question_for_tenant", _fake_next_question_for_tenant)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: next_required_question.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores"
            ),
            tool_name="next_required_question",
        )
    assert out["field"] == "city"
    assert seen["tenant_id"] == tenant_id


def test_next_required_question_foreign_uuid_from_model_overridden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.conductor as conductor_mod
    from orchestrator.agent.onboarding_conductor import next_required_question
    from orchestrator.onboarding.conductor import ConductorDecision

    seen: dict[str, Any] = {}

    def _fake_next_question_for_tenant(tid: Any) -> ConductorDecision:
        seen["tenant_id"] = tid
        return ConductorDecision(next_question=None, remaining=(), known=(), skipped=())

    monkeypatch.setattr(conductor_mod, "next_question_for_tenant", _fake_next_question_for_tenant)

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: next_required_question.func(  # type: ignore[attr-defined]
                tenant_id=str(foreign)
            ),
            tool_name="next_required_question",
        )
    assert out == {"done": True}
    assert seen["tenant_id"] == tenant_id
    assert seen["tenant_id"] != foreign


def test_next_required_question_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import next_required_question

    out = next_required_question.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores"
    )
    assert out == {
        "status": "error",
        "error": "next_required_question: no resolvable tenant context",
    }


# --- (2) profile_completion_check --------------------------------------------------------------


def test_profile_completion_check_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.conductor as conductor_mod
    import orchestrator.onboarding.draft_profile as draft_profile_mod
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import profile_completion_check

    seen: dict[str, Any] = {}

    def _fake_get_journey(tid: Any) -> dict[str, Any]:
        seen["get_journey_tenant"] = tid
        return {"answers": {"city": "Pune"}, "skipped": []}

    def _fake_tenant_phase_and_type(tid: Any) -> tuple[str | None, str | None]:
        seen["phase_type_tenant"] = tid
        return ("collecting", "retail")

    def _fake_get_draft(tid: Any) -> dict[str, Any]:
        seen["draft_tenant"] = tid
        return {}

    def _fake_profile_collection_complete(**kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(journey_mod, "get_journey", _fake_get_journey)
    monkeypatch.setattr(journey_mod, "_tenant_phase_and_type", _fake_tenant_phase_and_type)
    monkeypatch.setattr(draft_profile_mod, "get_draft", _fake_get_draft)
    monkeypatch.setattr(conductor_mod, "profile_collection_complete", _fake_profile_collection_complete)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: profile_completion_check.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores"
            ),
            tool_name="profile_completion_check",
        )
    assert out == {"complete": True}
    assert seen["get_journey_tenant"] == tenant_id
    assert seen["phase_type_tenant"] == tenant_id
    assert seen["draft_tenant"] == tenant_id


def test_profile_completion_check_foreign_uuid_from_model_overridden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.onboarding.conductor as conductor_mod
    import orchestrator.onboarding.draft_profile as draft_profile_mod
    import orchestrator.onboarding.journey as journey_mod
    from orchestrator.agent.onboarding_conductor import profile_completion_check

    seen: dict[str, Any] = {}

    def _fake_get_journey(tid: Any) -> dict[str, Any]:
        seen["get_journey_tenant"] = tid
        return {"answers": {}, "skipped": []}

    def _fake_tenant_phase_and_type(tid: Any) -> tuple[str | None, str | None]:
        return (None, None)

    def _fake_get_draft(tid: Any) -> dict[str, Any]:
        return {}

    def _fake_profile_collection_complete(**kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(journey_mod, "get_journey", _fake_get_journey)
    monkeypatch.setattr(journey_mod, "_tenant_phase_and_type", _fake_tenant_phase_and_type)
    monkeypatch.setattr(draft_profile_mod, "get_draft", _fake_get_draft)
    monkeypatch.setattr(conductor_mod, "profile_collection_complete", _fake_profile_collection_complete)

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: profile_completion_check.func(  # type: ignore[attr-defined]
                tenant_id=str(foreign)
            ),
            tool_name="profile_completion_check",
        )
    assert out == {"complete": False}
    assert seen["get_journey_tenant"] == tenant_id
    assert seen["get_journey_tenant"] != foreign


def test_profile_completion_check_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.onboarding_conductor import profile_completion_check

    out = profile_completion_check.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid"
    )
    assert out == {
        "status": "error",
        "error": "profile_completion_check: no resolvable tenant context",
    }
