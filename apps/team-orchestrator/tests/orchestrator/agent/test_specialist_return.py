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
    _enforce_enabled,
    handle_specialist_return,
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


# --- VT-554: uniform action envelope + config-gated enforce ------------------


def test_enforce_enabled_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("MANAGER_ENFORCE_ROUTING", raising=False)
    assert _enforce_enabled(None) is False
    monkeypatch.setenv("MANAGER_ENFORCE_ROUTING", "true")
    assert _enforce_enabled(None) is True
    assert _enforce_enabled(False) is False  # explicit arg overrides the env


def test_handle_action_envelope_is_observed(monkeypatch) -> None:
    import orchestrator.observability.tm_audit as tm
    recorded: list = []
    monkeypatch.setattr(tm, "emit_tm_audit", lambda **kw: recorded.append(kw))
    token = deco._observability_context.set(
        ObservabilityContext(run_id=uuid4(), tenant_id=uuid4())
    )
    try:
        d = handle_specialist_return(
            {"pushback": False, "action_taken": "recommended winback play", "outcome": "38 dormant"},
            agent="sales",
        )
    finally:
        deco._observability_context.reset(token)
    assert d is not None and d.kind is ManagerDecisionKind.ACCEPT  # action, no next step → accept
    assert recorded and recorded[0]["decision"]["pushback"] is False


def test_handle_enforce_escalate_opens_incident(monkeypatch) -> None:
    import orchestrator.observability.incident_store as inc
    import orchestrator.observability.tm_audit as tm
    monkeypatch.setattr(tm, "emit_tm_audit", lambda **kw: None)
    opened: list = []
    escalated: list = []
    monkeypatch.setattr(inc, "create_incident", lambda tid, **kw: opened.append(kw) or uuid4())
    monkeypatch.setattr(inc, "escalate_incident", lambda tid, iid, **kw: escalated.append(kw) or True)

    token = deco._observability_context.set(
        ObservabilityContext(run_id=uuid4(), tenant_id=uuid4())
    )
    try:
        # pushback with NO proposed_outcome → ESCALATE; enforce=True → deterministic incident.
        d = handle_specialist_return(
            {"pushback": True, "reason": "no path in lane"}, agent="sales", enforce=True
        )
    finally:
        deco._observability_context.reset(token)
    assert d is not None and d.kind is ManagerDecisionKind.ESCALATE
    assert len(opened) == 1 and opened[0]["incident_kind"] == "other"
    assert len(escalated) == 1 and escalated[0]["to_tier"] == 2


def test_handle_no_enforce_does_not_open_incident(monkeypatch) -> None:
    import orchestrator.observability.incident_store as inc
    import orchestrator.observability.tm_audit as tm
    monkeypatch.setattr(tm, "emit_tm_audit", lambda **kw: None)
    opened: list = []
    monkeypatch.setattr(inc, "create_incident", lambda tid, **kw: opened.append(kw) or uuid4())

    token = deco._observability_context.set(
        ObservabilityContext(run_id=uuid4(), tenant_id=uuid4())
    )
    try:
        d = handle_specialist_return(
            {"pushback": True, "reason": "no path"}, agent="sales", enforce=False
        )
    finally:
        deco._observability_context.reset(token)
    assert d is not None and d.kind is ManagerDecisionKind.ESCALATE
    assert opened == []  # observe-only default: no deterministic escalation
