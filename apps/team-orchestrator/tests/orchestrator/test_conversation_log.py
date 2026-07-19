"""VT-579 — unit coverage for the conversation-memory seams (no DB, no live LLM).

Realdb coverage (record/window/24h-cutoff/20-cap/idempotent-sid/search/DSR/compaction/journey double-write)
lives in ``test_conversation_log_realdb.py``. This file pins the PURE / mockable seams:
  - the ALWAYS-ON dispatch conversation block (rendered from a mocked window + summary — no env gate),
  - the manager ``search_conversation_history`` tool (tenant from ambient context; no-context → honest error),
  - the onboarding turn-brain ``search_conversation_history`` client tool (schema + payload + routing),
  - the pure helpers (``_iso`` / ``_row_to_turn``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

# conversation_log imports dbos (the @DBOS.workflow); dispatch/turn_brain import the langchain stack.
pytest.importorskip("dbos")
pytest.importorskip("psycopg")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")

from orchestrator import conversation_log as cl  # noqa: E402 — after dependency skip guards


# --- pure helpers --------------------------------------------------------------------------------


def test_iso_renders_datetime_and_passes_through_str():
    dt = datetime(2026, 7, 3, 10, 30, tzinfo=UTC)
    assert cl._iso(dt) == dt.isoformat()
    assert cl._iso("2026-07-03T10:30:00+00:00") == "2026-07-03T10:30:00+00:00"
    assert cl._iso(None) is None
    assert cl._iso(123) is None


def test_row_to_turn_dict_and_tuple():
    dt = datetime(2026, 7, 3, tzinfo=UTC)
    d = cl._row_to_turn({"role": "owner", "text": "hi", "created_at": dt, "surface": "manager"})
    assert d == {"role": "owner", "text": "hi", "created_at": dt, "surface": "manager"}
    t = cl._row_to_turn(("assistant", "yo", dt, "journey"))
    assert t == {"role": "assistant", "text": "yo", "created_at": dt, "surface": "journey"}


# --- the ALWAYS-ON dispatch conversation block ---------------------------------------------------


def test_conversation_block_is_always_on_and_renders(monkeypatch):
    """No env gate (unlike VTR-directive/lessons): the block renders whenever there is a window/summary,
    even with MANAGER_MEMORY_RETRIEVAL unset. Owner/assistant labeled; summary carried above the turns."""
    from orchestrator.agent import dispatch

    monkeypatch.delenv("MANAGER_MEMORY_RETRIEVAL", raising=False)
    monkeypatch.setattr(cl, "read_manager_summary", lambda t: "Runs a Pune electronics shop.")
    monkeypatch.setattr(
        cl,
        "active_window",
        lambda t, **k: [
            {"role": "owner", "text": "hi there", "created_at": None, "surface": "manager"},
            {"role": "assistant", "text": "hello! how can I help?", "created_at": None, "surface": "manager"},
        ],
    )
    block = dispatch._build_manager_conversation_block(uuid4())
    assert block is not None
    assert "## Conversation (last 24h)" in block
    assert "Earlier (summarised): Runs a Pune electronics shop." in block
    assert "owner: hi there" in block
    assert "assistant: hello! how can I help?" in block


def test_conversation_block_none_when_empty(monkeypatch):
    from orchestrator.agent import dispatch

    monkeypatch.setattr(cl, "read_manager_summary", lambda t: None)
    monkeypatch.setattr(cl, "active_window", lambda t, **k: [])
    assert dispatch._build_manager_conversation_block(uuid4()) is None


def test_conversation_block_forwards_exclude_sid(monkeypatch):
    """The current inbound sid is threaded through to active_window so it is not double-shown (it rides
    as the HumanMessage)."""
    from orchestrator.agent import dispatch

    seen = {}
    monkeypatch.setattr(cl, "read_manager_summary", lambda t: None)

    def _aw(t, **k):
        seen["exclude"] = k.get("exclude_message_sid")
        return [{"role": "owner", "text": "x", "created_at": None, "surface": "manager"}]

    monkeypatch.setattr(cl, "active_window", _aw)
    dispatch._build_manager_conversation_block(uuid4(), exclude_message_sid="SM123")
    assert seen["exclude"] == "SM123"


def test_conversation_block_fail_soft(monkeypatch):
    from orchestrator.agent import dispatch

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cl, "read_manager_summary", _boom)
    monkeypatch.setattr(cl, "active_window", _boom)
    assert dispatch._build_manager_conversation_block(uuid4()) is None


# --- dispatch_brain injects the window + records conversation_present in the GETS audit -----------


def test_dispatch_brain_injects_conversation_window(monkeypatch):
    """dispatch_brain assembles the ## Conversation block into the graph's initial_state messages (a
    per-turn SystemMessage after the cached prefix), ALWAYS ON, and stamps conversation_present=True on
    the GETS retrieval audit row."""
    from langchain_core.messages import SystemMessage

    from orchestrator.agent import dispatch
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant_id = uuid4()
    run_id = uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-sentinel")
    monkeypatch.delenv("MANAGER_MEMORY_RETRIEVAL", raising=False)

    # conversation memory: a real window.
    monkeypatch.setattr(cl, "read_manager_summary", lambda t: None)
    monkeypatch.setattr(
        cl,
        "active_window",
        lambda t, **k: [{"role": "owner", "text": "what's my plan?", "created_at": None, "surface": "manager"}],
    )

    # edge router: fall through to the agent, no classification.
    import orchestrator.edge_cases_router as ecr

    monkeypatch.setattr(ecr, "route_edge_case", lambda **k: None)

    # capture the GETS audit rows.
    audits: list[dict] = []
    monkeypatch.setattr(dispatch, "emit_tm_audit", lambda **k: audits.append(k))

    # null out the observability context + task-close + checkpointer (no DB).
    import contextlib

    monkeypatch.setattr(dispatch, "observability_context", lambda **k: contextlib.nullcontext())
    import orchestrator.manager.task_producer as tp

    monkeypatch.setattr(tp, "on_run_completed", lambda *a, **k: None)
    import orchestrator.graph as graph_mod

    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)

    # fake supervisor graph: capture the initial_state the brain hands it.
    captured: dict = {}

    class _FakeGraph:
        def invoke(self, initial_state, config):
            captured["messages"] = list(initial_state["messages"])
            return {"terminated_without_spawn": True, "messages": []}

    monkeypatch.setattr(dispatch, "build_supervisor_graph", lambda **k: _FakeGraph())

    event = WebhookEvent(
        body="what's my plan?", sender_phone="+910000000000",
        message_type="inbound_message", twilio_message_sid="SM_current",
    )
    state = new_subscriber_state(tenant_id, run_id)

    result = dispatch.dispatch_brain(event=event, state=state, run_id=run_id, tenant_id=tenant_id)
    assert result.final_status == "completed"

    # the ## Conversation block reached the graph as a SystemMessage.
    sys_texts = [m.content for m in captured["messages"] if isinstance(m, SystemMessage)]
    assert any("## Conversation (last 24h)" in t for t in sys_texts)
    assert any("owner: what's my plan?" in t for t in sys_texts)

    # the current inbound sid was excluded from the window read.
    # (active_window is mocked, but the block builder forwards exclude_message_sid — covered above.)

    # GETS retrieval audit carries conversation_present=True.
    retrieval = next(a for a in audits if a.get("event_kind") == "retrieval")
    assert retrieval["result"]["conversation_present"] is True


# --- the manager search tool -----------------------------------------------------------------------


def test_manager_search_tool_no_context_is_honest_error():
    from orchestrator.agent.orchestrator_agent import search_conversation_history

    out = search_conversation_history.invoke({"query": "pricing"})
    assert out["status"] == "error"
    assert out["matches"] == []


def test_manager_search_tool_uses_ambient_tenant(monkeypatch):
    from orchestrator.agent.orchestrator_agent import search_conversation_history
    from orchestrator.observability import decorators as dec

    tenant_id = uuid4()
    dt = datetime(2026, 7, 3, tzinfo=UTC)
    monkeypatch.setattr(
        cl, "search_history",
        lambda t, q, **k: [{"role": "owner", "text": "we sell sarees", "created_at": dt, "surface": "manager"}],
    )
    ctx = dec.ObservabilityContext(run_id=uuid4(), tenant_id=tenant_id)
    token = dec._observability_context.set(ctx)
    try:
        out = search_conversation_history.invoke({"query": "sarees"})
    finally:
        dec._observability_context.reset(token)
    assert out["status"] == "ok"
    assert out["matches"][0]["text"] == "we sell sarees"
    assert out["matches"][0]["at"] == dt.isoformat()


# --- the onboarding turn-brain search client tool -------------------------------------------------


def test_turn_brain_search_payload_and_routing(monkeypatch):
    from orchestrator.onboarding import turn_brain as tb

    dt = datetime(2026, 7, 3, tzinfo=UTC)
    monkeypatch.setattr(
        cl, "search_history",
        lambda t, q, **k: [{"role": "assistant", "text": "your GST is registered", "created_at": dt, "surface": "journey"}],
    )
    # no tenant → empty (client tool needs a tenant).
    assert json.loads(tb._search_conversation_payload(None, "gst"))["matches"] == []
    # with tenant → the matches JSON.
    payload = json.loads(tb._search_conversation_payload(uuid4(), "gst"))
    assert payload["matches"][0]["text"] == "your GST is registered"

    # the tool schema is well-formed.
    schema = tb._search_conversation_tool()
    assert schema["name"] == "search_conversation_history"
    assert "query" in schema["input_schema"]["properties"]

    # _handle_client_tool_uses routes the tool_use block to the payload.
    from types import SimpleNamespace

    block = SimpleNamespace(type="tool_use", name="search_conversation_history", id="tu1", input={"query": "gst"})
    results = tb._handle_client_tool_uses(
        [block], journey_state={}, provenance=None, pinnable_domains=[], tenant_id=uuid4()
    )
    assert results[0]["tool_use_id"] == "tu1"
    assert "your GST is registered" in results[0]["content"]
