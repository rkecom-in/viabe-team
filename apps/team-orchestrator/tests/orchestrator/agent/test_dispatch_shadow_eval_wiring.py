"""VT-611 (Phase B2, Finding A) — the shadow_eval wiring inside ``dispatch_brain`` itself.

Mirrors ``test_dispatch_collapse_wiring.py``'s own stubbed-graph harness exactly (no DB, no LLM —
the graph is a fake, every DB-touching side read is fail-soft/unwired). Proves the THREE binding
constraints from the team-lead's review gate:

  1. Mode-gated: legacy (the default) NEVER calls ``evaluate_turn_shadow`` — and never even
     IMPORTS ``orchestrator.manager.shadow_eval`` (byte-identical import-time cost too).
  2. Fail-soft: a raising ``evaluate_turn_shadow`` never propagates out of ``dispatch_brain`` — the
     real turn's own ``DispatchResult`` is unaffected.
  3. Shadow-mode call shape: collapse (a real CampaignPlan) threads ``campaign_plan``; terminal
     (a direct answer) threads ``raw_output``; the VT-241 cohort-rejected variant and the
     escalate_to_fazal-tool variant are correctly SKIPPED (nothing new to evaluate — see the
     wiring's own comment for why).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")
pytest.importorskip("anthropic")

from langchain_core.messages import AIMessage  # noqa: E402


def _drive(monkeypatch, terminal_state: dict, *, shadow_eval_calls: list | None = None):
    """Same scaffold as test_dispatch_collapse_wiring.py's ``_drive`` — a fake graph returns
    ``terminal_state`` directly, no DB, no LLM. Additionally records ``evaluate_turn_shadow`` calls
    (patched at its DEFINING module — the wiring's lazy ``from ... import evaluate_turn_shadow``
    resolves the CURRENT attribute value at call time, so patching the source module works
    regardless of the lazy-import timing)."""
    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    import orchestrator.manager.shadow_eval as shadow_eval_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant_id, run_id = uuid4(), uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt611-wiring-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())
    monkeypatch.setattr(dispatch_mod, "_maybe_send_collapse_reply", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "_maybe_send_manager_reply", lambda *a, **k: None)

    calls = shadow_eval_calls if shadow_eval_calls is not None else []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(divergence_class="no_divergence")

    monkeypatch.setattr(shadow_eval_mod, "evaluate_turn_shadow", _record)

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            return terminal_state

    monkeypatch.setattr(dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph())

    event = WebhookEvent(
        body="what's my last order total",
        sender_phone="+10000000000",
        message_type="inbound_message",
        twilio_message_sid="SMvt611shadowwiring",
    )
    state = new_subscriber_state(tenant_id, run_id)
    result = dispatch_brain(event=event, state=state, run_id=run_id, tenant_id=tenant_id)
    return result, calls, tenant_id, run_id


def _collapse_state():
    from orchestrator.agent.schemas.campaign_plan import CampaignPlanOutOfScope

    plan = CampaignPlanOutOfScope(
        tenant_id=uuid4(), run_id=uuid4(), generated_at=datetime.now(UTC),
        out_of_scope_reason="Wiring-test out-of-scope reason.",
    )
    return {"campaign_plan": plan}, plan


# ---------------------------------------------------------------------------
# 1. Legacy (the default) — zero calls, zero import cost.
# ---------------------------------------------------------------------------


def test_legacy_mode_never_calls_shadow_eval(monkeypatch):
    monkeypatch.delenv("TEAM_MANAGER_LOOP_MODE", raising=False)
    state, _plan = _collapse_state()
    result, calls, _tid, _rid = _drive(monkeypatch, state)
    assert result.final_status == "completed"
    assert calls == []


def test_legacy_mode_never_imports_shadow_eval_module(monkeypatch):
    import sys

    monkeypatch.delenv("TEAM_MANAGER_LOOP_MODE", raising=False)
    sys.modules.pop("orchestrator.manager.shadow_eval", None)
    before = set(sys.modules)
    state, _plan = _collapse_state()
    # Cannot use _drive here (it imports shadow_eval itself to install the patch) — drive the
    # bare minimum directly to prove the WIRING never imports it, not the test harness.
    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant_id, run_id = uuid4(), uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt611-wiring-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())
    monkeypatch.setattr(dispatch_mod, "_maybe_send_collapse_reply", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "_maybe_send_manager_reply", lambda *a, **k: None)

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            return state

    monkeypatch.setattr(dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph())
    event = WebhookEvent(
        body="hi", sender_phone="+10000000000",
        message_type="inbound_message", twilio_message_sid="SMvt611legacyimport",
    )
    dispatch_brain(
        event=event, state=new_subscriber_state(tenant_id, run_id), run_id=run_id, tenant_id=tenant_id
    )
    newly_imported = set(sys.modules) - before
    assert "orchestrator.manager.shadow_eval" not in newly_imported


# ---------------------------------------------------------------------------
# 2. Shadow mode — the right calls, the right shape.
# ---------------------------------------------------------------------------


def test_shadow_mode_collapse_completed_calls_shadow_eval_with_campaign_plan(monkeypatch):
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    state, plan = _collapse_state()
    result, calls, tenant_id, run_id = _drive(monkeypatch, state)

    assert result.final_status == "completed"
    assert result.terminal_path == "collapse"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == tenant_id
    assert kwargs["campaign_plan"] is plan
    assert kwargs["legacy_final_status"] == "completed"
    assert kwargs["run_id"] == run_id
    assert kwargs["turn_ref"] == "SMvt611shadowwiring"


def test_shadow_mode_terminal_completed_calls_shadow_eval_with_raw_output(monkeypatch):
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    result, calls, _tid, _rid = _drive(
        monkeypatch,
        {
            "terminated_without_spawn": True,
            "messages": [AIMessage(content="Your last order total was ₹450.")],
        },
    )
    assert result.final_status == "completed"
    assert result.terminal_path == "terminal"
    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert kwargs["campaign_plan"] is None
    assert "₹450" in kwargs["raw_output"]


def test_shadow_mode_cohort_rejected_skips_shadow_eval(monkeypatch):
    """The VT-241 fail-closed cohort-rejection variant (_CohortRejectedResult, not a real
    CampaignPlan) — legacy's OWN rail already rejected it; nothing new to evaluate."""
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    result, calls, _tid, _rid = _drive(
        monkeypatch, {"campaign_rejected": {"rejected_count": 3}}
    )
    assert result.final_status == "completed"
    assert result.terminal_path == "collapse"
    assert calls == []


def test_shadow_mode_escalated_via_tool_skips_shadow_eval(monkeypatch):
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    result, calls, _tid, _rid = _drive(
        monkeypatch,
        {"messages": [SimpleNamespace(name="escalate_to_fazal", content="needs human")]},
    )
    assert result.final_status == "escalated"
    assert calls == []


def test_shadow_mode_paused_skips_shadow_eval(monkeypatch):
    """Paused returns from dispatch_brain BEFORE the wiring's own insertion point — never reached."""
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    result, calls, _tid, _rid = _drive(monkeypatch, {"__interrupt__": ["sentinel"]})
    assert result.final_status == "paused"
    assert calls == []


# ---------------------------------------------------------------------------
# 3. Fail-soft — a raising evaluate_turn_shadow never touches the real turn.
# ---------------------------------------------------------------------------


def test_shadow_mode_shadow_eval_failure_never_propagates(monkeypatch):
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "shadow")
    import orchestrator.manager.shadow_eval as shadow_eval_mod

    def _boom(*a, **k):
        raise RuntimeError("shadow_eval blew up")

    state, _plan = _collapse_state()
    # Install the raiser AFTER _drive's own patch runs, by patching post-hoc via a second drive
    # call that overrides it — simplest: drive with a pre-set patch instead of _drive's recorder.
    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant_id, run_id = uuid4(), uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt611-wiring-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())
    monkeypatch.setattr(dispatch_mod, "_maybe_send_collapse_reply", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "_maybe_send_manager_reply", lambda *a, **k: None)
    monkeypatch.setattr(shadow_eval_mod, "evaluate_turn_shadow", _boom)

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            return state

    monkeypatch.setattr(dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph())
    event = WebhookEvent(
        body="hi", sender_phone="+10000000000",
        message_type="inbound_message", twilio_message_sid="SMvt611failsoft",
    )
    result = dispatch_brain(
        event=event, state=new_subscriber_state(tenant_id, run_id), run_id=run_id, tenant_id=tenant_id
    )
    # Reaching here (no raised RuntimeError) IS the proof — the real turn's own result is intact.
    assert result.final_status == "completed"
    assert result.terminal_path == "collapse"
