"""VT-606 (team-lead ruling round 2) — the opus plan-validation checkpoint (mocked client, no
network)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("anthropic")

from orchestrator.manager.plan_models import ManagerPlan, PlanStep  # noqa: E402
from orchestrator.manager.plan_validation import validate_plan_draft  # noqa: E402


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, content: list) -> None:
        self.content = content


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    @property
    def messages(self):
        payload = self._payload

        class _M:
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock(json.dumps(payload))])

        return _M()


def _plan() -> ManagerPlan:
    return ManagerPlan(
        objective="win back lapsed customers",
        acceptance_criteria=["3+ customers recovered within 7 days"],
        steps=[PlanStep(step_seq=1, kind="specialist_dispatch", specialist="sales_recovery_agent")],
    )


def test_valid_plan() -> None:
    result = validate_plan_draft(
        _plan(), client=_FakeClient({"valid": True, "reason": "criteria are measurable"})
    )
    assert result.valid is True


def test_invalid_plan_vague_criteria() -> None:
    result = validate_plan_draft(
        _plan(), client=_FakeClient({"valid": False, "reason": "criteria not measurable"})
    )
    assert result.valid is False
    assert result.reason


def test_fail_soft_on_non_json_response() -> None:
    class _RawTextClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                return _FakeResp([_FakeTextBlock("not json")])

    result = validate_plan_draft(_plan(), client=_RawTextClient())
    assert result.valid is False
    assert "plan_validation_extraction_failed" in result.reason


def test_fail_soft_on_client_exception() -> None:
    class _RaisingClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):  # noqa: ANN003, ANN201
                raise RuntimeError("network down")

    result = validate_plan_draft(_plan(), client=_RaisingClient())
    assert result.valid is False


def test_fail_soft_on_schema_mismatch() -> None:
    result = validate_plan_draft(_plan(), client=_FakeClient({"valid": "not_a_bool"}))
    assert result.valid is False
    assert "plan_validation_extraction_failed" in result.reason
