"""VT-594 (post-review test gap #1) — dispatch_brain call-site wiring.

The double-send/false-promise findings in the adversarial review traced back
to the ORIGINAL diff never having a test that actually drove ``dispatch_brain``
end-to-end through each terminal shape and asserted WHICH owner-reply seam
fires. This file closes that gap: a stubbed ``graph.invoke`` returns each of
the four terminal shapes ``_classify_terminal`` / the pre-classify ``__interrupt__``
check recognise, and we assert the call-site gating in ``dispatch_brain`` is
mutually exclusive — collapse+completed calls ONLY ``_maybe_send_collapse_
reply``; terminal+completed calls ONLY ``_maybe_send_manager_reply``; paused
and escalated call NEITHER (support_bot / the interrupt itself own those).

Mirrors the existing stubbing pattern in ``test_dispatch_classify.py`` (VT-492
``test_dispatch_brain_specialist_no_output_resolves_to_clean_escalated``, VT-566
``test_gets_retrieval_audit_carries_lessons_present``): no DB, no LLM — the
graph itself is a fake, and every DB-touching side read (L1 context, business
context, task_producer transitions) is either unwired (no pool) or fail-soft.
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

from langchain_core.messages import AIMessage  # noqa: E402


def _drive(monkeypatch, terminal_state: dict):
    """Wire the standard no-DB/no-LLM scaffold and invoke dispatch_brain against
    a fake graph that returns ``terminal_state`` directly. Returns
    (dispatch_result, collapse_calls, manager_calls)."""
    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant_id, run_id = uuid4(), uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt594-wiring-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            return terminal_state

    monkeypatch.setattr(dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph())

    collapse_calls: list[tuple] = []
    manager_calls: list[tuple] = []
    monkeypatch.setattr(
        dispatch_mod,
        "_maybe_send_collapse_reply",
        lambda *a, **k: collapse_calls.append((a, k)),
    )
    monkeypatch.setattr(
        dispatch_mod,
        "_maybe_send_manager_reply",
        lambda *a, **k: manager_calls.append((a, k)),
    )

    event = WebhookEvent(
        body="make me a plan to win back my lapsed customers",
        sender_phone="+10000000000",
        message_type="inbound_message",
        twilio_message_sid="SMvt594wiring",
    )
    state = new_subscriber_state(tenant_id, run_id)
    result = dispatch_brain(event=event, state=state, run_id=run_id, tenant_id=tenant_id)
    return result, collapse_calls, manager_calls


def test_collapse_completed_calls_only_collapse_reply_with_classified_result(monkeypatch):
    from orchestrator.agent.schemas.campaign_plan import CampaignPlanOutOfScope

    plan = CampaignPlanOutOfScope(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        out_of_scope_reason="Wiring-test out-of-scope reason.",
    )
    result, collapse_calls, manager_calls = _drive(monkeypatch, {"campaign_plan": plan})

    assert result.final_status == "completed"
    assert result.terminal_path == "collapse"
    assert len(collapse_calls) == 1, "_maybe_send_collapse_reply must fire exactly once"
    # args: (tenant_id, event, terminal_state, specialist_result)
    assert collapse_calls[0][0][3] is plan, (
        "the classified specialist_result must be threaded through unchanged"
    )
    assert manager_calls == [], "the disjoint terminal-path seam must NOT also fire"


def test_terminal_completed_calls_only_manager_reply(monkeypatch):
    result, collapse_calls, manager_calls = _drive(
        monkeypatch,
        {
            "terminated_without_spawn": True,
            "messages": [AIMessage(content="Direct handle answer.")],
        },
    )

    assert result.final_status == "completed"
    assert result.terminal_path == "terminal"
    assert len(manager_calls) == 1
    assert collapse_calls == [], "the disjoint collapse-path seam must NOT also fire"


def test_paused_calls_neither_seam(monkeypatch):
    result, collapse_calls, manager_calls = _drive(
        monkeypatch, {"__interrupt__": ["sentinel"]}
    )

    assert result.final_status == "paused"
    assert result.terminal_path == "paused"
    assert collapse_calls == []
    assert manager_calls == []


def test_escalated_calls_neither_seam(monkeypatch):
    result, collapse_calls, manager_calls = _drive(
        monkeypatch,
        {"messages": [SimpleNamespace(name="escalate_to_fazal", content="needs human")]},
    )

    assert result.final_status == "escalated"
    assert result.terminal_path == "escalated"
    assert collapse_calls == []
    assert manager_calls == []
