"""VT-606 (team-lead ruling round 2) — the opus plan-validation checkpoint (mocked client, no
network)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from orchestrator.manager.plan_models import ManagerPlan, PlanStep  # noqa: E402
from orchestrator.manager.plan_validation import validate_plan_draft  # noqa: E402


def _json_call(payload: dict):
    """A ``text_call`` stub returning ``payload`` as JSON text. Mirrors ``structured_text_call``'s
    signature ``(tier, *, system, user, max_tokens, agent, call_site, tenant_id)`` — accepts and
    ignores whatever the site passes."""

    def _call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        return json.dumps(payload)

    return _call


def _text_call(raw: str):
    def _call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        return raw

    return _call


def _raising_call(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
    raise RuntimeError("network down")


def _plan() -> ManagerPlan:
    return ManagerPlan(
        objective="win back lapsed customers",
        acceptance_criteria=["3+ customers recovered within 7 days"],
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent")],
    )


def test_valid_plan() -> None:
    result = validate_plan_draft(
        _plan(), text_call=_json_call({"valid": True, "reason": "criteria are measurable"})
    )
    assert result.valid is True


def test_invalid_plan_vague_criteria() -> None:
    result = validate_plan_draft(
        _plan(), text_call=_json_call({"valid": False, "reason": "criteria not measurable"})
    )
    assert result.valid is False
    assert result.reason


def test_fail_soft_on_non_json_response() -> None:
    result = validate_plan_draft(_plan(), text_call=_text_call("not json"))
    assert result.valid is False
    assert "plan_validation_extraction_failed" in result.reason


def test_fail_soft_on_client_exception() -> None:
    result = validate_plan_draft(_plan(), text_call=_raising_call)
    assert result.valid is False


def test_fail_soft_on_schema_mismatch() -> None:
    result = validate_plan_draft(_plan(), text_call=_json_call({"valid": "not_a_bool"}))
    assert result.valid is False
    assert "plan_validation_extraction_failed" in result.reason
