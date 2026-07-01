"""VT-526 (B3) graph-wiring — the specialist→manager return bridge.

Proves the bridge parses a real specialist return envelope into a ``SpecialistReturn``, runs
``decide_next_action`` on it, and records the manager decision to tm_audit OBSERVE-ONLY (no routing
change) — and is fully fail-soft (no context / unrecognized envelope → no emit, no raise).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

# Constructing SpecialistReturn (inside parse) pulls roster → langgraph; skip under the dep-less smoke.
pytest.importorskip("langgraph")

from orchestrator.agent.specialist_return import (  # noqa: E402
    observe_specialist_return,
    parse_specialist_return,
)
from orchestrator.manager.decision import ManagerDecisionKind  # noqa: E402
from orchestrator.observability import decorators as deco  # noqa: E402
from orchestrator.observability.decorators import ObservabilityContext  # noqa: E402


def test_parse_pushback() -> None:
    sr = parse_specialist_return(
        {"kind": "sales_lane_pushback", "pushback": True,
         "reason": "no consent on cohort", "proposed_outcome": "nurture first"}
    )
    assert sr is not None and sr.pushback is True
    assert sr.proposed_outcome == "nurture first"


def test_parse_action() -> None:
    sr = parse_specialist_return({"action_taken": "queued winback", "outcome": "awaiting approval"})
    assert sr is not None and sr.pushback is False
    assert sr.action_taken == "queued winback"


def test_parse_unrecognized_is_none() -> None:
    assert parse_specialist_return({"foo": "bar"}) is None
    assert parse_specialist_return("nope") is None
    assert parse_specialist_return(None) is None


def test_observe_pushback_with_proposed_decides_revise_and_records(monkeypatch) -> None:
    import orchestrator.observability.tm_audit as tm
    recorded: list = []
    monkeypatch.setattr(tm, "emit_tm_audit", lambda **kw: recorded.append(kw))

    token = deco._observability_context.set(
        ObservabilityContext(run_id=uuid4(), tenant_id=uuid4())
    )
    try:
        d = observe_specialist_return(
            {"pushback": True, "reason": "x", "proposed_outcome": "better"}, agent="sales_lane"
        )
    finally:
        deco._observability_context.reset(token)

    assert d is not None and d.kind is ManagerDecisionKind.REVISE
    assert len(recorded) == 1
    assert recorded[0]["event_kind"] == "manager_decision"
    assert recorded[0]["status"] == "observed"
    assert recorded[0]["decision"]["kind"] == "revise"


def test_observe_pushback_no_proposed_escalates(monkeypatch) -> None:
    import orchestrator.observability.tm_audit as tm
    monkeypatch.setattr(tm, "emit_tm_audit", lambda **kw: None)
    token = deco._observability_context.set(
        ObservabilityContext(run_id=uuid4(), tenant_id=uuid4())
    )
    try:
        d = observe_specialist_return({"pushback": True, "reason": "infeasible"}, agent="sales_lane")
    finally:
        deco._observability_context.reset(token)
    assert d is not None and d.kind is ManagerDecisionKind.ESCALATE


def test_observe_no_context_returns_decision_but_no_emit(monkeypatch) -> None:
    import orchestrator.observability.tm_audit as tm
    recorded: list = []
    monkeypatch.setattr(tm, "emit_tm_audit", lambda **kw: recorded.append(kw))

    token = deco._observability_context.set(None)  # deterministic: no run context
    try:
        d = observe_specialist_return(
            {"pushback": True, "proposed_outcome": "p"}, agent="sales_lane"
        )
    finally:
        deco._observability_context.reset(token)
    assert d is not None and d.kind is ManagerDecisionKind.REVISE
    assert recorded == []  # observe-only + no context → no audit row


def test_observe_unrecognized_returns_none() -> None:
    assert observe_specialist_return({"foo": "bar"}, agent="sales_lane") is None
