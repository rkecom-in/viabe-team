"""VT-570 — unit tests for the turn-brain TOOL BELT (bounded agentic loop, MOCKED Anthropic).

No network, no DB: the loop's model-call seam ``_invoke_llm_tools`` is monkeypatched to return canned
response objects, so the loop mechanics (client-tool dispatch → tool_result → re-call → final parse),
the iteration cap, host-pinning, and the read_journey_history payload are all exercised deterministically.
The tenant_id gate (present → loop; absent → the classic VT-569 single call, exercised by the untouched
``test_turn_brain.py``) is pinned here too, so the two paths never diverge.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.onboarding import turn_brain
from orchestrator.onboarding.turn_brain import TurnPlan, compose_turn


def _text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, tool_id: str, tool_input: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(type="tool_use", name=name, id=tool_id, input=tool_input or {})


def _resp(stop_reason: str, *blocks: Any) -> Any:
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


_FINAL = {
    "reply_text": "All set — thanks!", "buttons": [], "extracted_answers": {},
    "mark_confirmed": [], "mark_rejected": [], "done_hint": False, "reasoning": "done",
}

_STATE: dict[str, Any] = {
    "question_queue": [{"field": "operating_hours", "kind": "gap", "prompt_en": "What are your hours?"}],
    "cursor": 0, "answers": {"city": "Pune"}, "skipped": [],
    "recent_turns": [{"role": "owner", "text": "hi"}, {"role": "bot", "text": "hello"}],
}


def test_loop_executes_client_tool_then_finalizes(monkeypatch):
    """The brain calls read_journey_history; we answer it; the next call returns the final JSON.

    Pins the whole round-trip: client tool_use is dispatched, its tool_result is fed back on the next
    model call, and the final text parses into an unchanged TurnPlan."""
    calls: list[dict[str, Any]] = []

    def _fake(system_prompt, messages, tools, betas):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            return _resp("tool_use", _tool_use_block("read_journey_history", "tu_1"))
        return _resp("end_turn", _text_block(json.dumps(_FINAL)))

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)
    plan = compose_turn(_STATE, {}, "how's it going?", locale="en", tenant_id="t-1")
    assert isinstance(plan, TurnPlan)
    assert plan.reply_text == "All set — thanks!"
    assert len(calls) == 2, "one tool round-trip, then the final call"
    # the read_journey_history tool_result was fed back on the 2nd call and carries the window + answers
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    payload = json.loads(tool_result["content"])
    assert payload["answers"] == {"city": "Pune"}
    assert payload["recent_turns"] and payload["recent_turns"][0]["text"] == "hi"


def test_iteration_cap_forces_finalization(monkeypatch):
    """A model that keeps requesting tools is capped: after ``_MAX_TOOL_ITERS`` round-trips the loop
    forces a final NO-TOOLS call (``tools == []``) and parses that answer."""
    calls: list[list[Any]] = []

    def _fake(system_prompt, messages, tools, betas):
        calls.append(tools)
        if tools:  # still offering tools → the model keeps requesting one
            return _resp("tool_use", _tool_use_block("read_journey_history", f"tu_{len(calls)}"))
        return _resp("end_turn", _text_block(json.dumps(_FINAL)))  # forced final (tools == [])

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)
    plan = compose_turn(_STATE, {}, "hi", locale="en", tenant_id="t-1")
    assert isinstance(plan, TurnPlan)
    assert calls[-1] == [], "the final call is forced with no tools"
    # initial call + _MAX_TOOL_ITERS round-trips + 1 forced final
    assert len(calls) == turn_brain._MAX_TOOL_ITERS + 2


def test_immediate_final_no_tool_call(monkeypatch):
    """Most turns need no tool: a first response with no tool_use parses straight through (one call)."""
    calls: list[Any] = []

    def _fake(system_prompt, messages, tools, betas):
        calls.append(tools)
        return _resp("end_turn", _text_block(json.dumps(_FINAL)))

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)
    plan = compose_turn(_STATE, {}, "just chatting", locale="en", tenant_id="t-1")
    assert isinstance(plan, TurnPlan) and plan.reply_text == "All set — thanks!"
    assert len(calls) == 1


def test_web_fetch_and_refresh_offered_only_when_domain_pinnable(monkeypatch):
    """web_fetch + refresh_discovery are offered only when the owner's own domains are pinnable (draft
    website or a URL in the message); read_journey_history is ALWAYS on."""
    captured: dict[str, list[str]] = {}

    def _fake(system_prompt, messages, tools, betas):
        captured["names"] = [t.get("type") or t.get("name") for t in tools]
        return _resp("end_turn", _text_block(json.dumps(_FINAL)))

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)

    compose_turn(_STATE, {"website": "https://mysite.in"}, "hi", locale="en", tenant_id="t-1")
    names = captured["names"]
    assert any("web_fetch" in str(n) for n in names), "web_fetch offered when a domain is pinnable"
    assert "refresh_discovery" in names
    assert "read_journey_history" in names

    compose_turn(_STATE, {}, "hi", locale="en", tenant_id="t-1")  # no website, no URL in body
    names = captured["names"]
    assert not any("web_fetch" in str(n) for n in names), "no web_fetch without a pinnable domain"
    assert "refresh_discovery" not in names
    assert "read_journey_history" in names, "read_journey_history is always on"


def test_refresh_discovery_rejects_unpinned_host():
    """The host guard: a URL whose host is NOT one of the owner's pinned domains is rejected and the
    durable workflow is never fired (the brain can never refresh an arbitrary site)."""
    out = turn_brain._refresh_discovery("https://evil.example/x", ["mysite.in"], "t-1")
    assert "rejected" in out.lower()
    # a tenant-less pinned call acknowledges without firing the workflow (no DBOS dependency touched)
    ok = turn_brain._refresh_discovery("https://mysite.in/about", ["mysite.in"], None)
    assert "mysite.in" in ok and "rejected" not in ok.lower()


def test_read_journey_history_payload_shape():
    """read_journey_history returns the window + answers + skipped + provenance (source+fetched_at)."""
    out = turn_brain._read_journey_history_payload(
        _STATE, {"business_type": {"source": "gbp", "fetched_at": "2026-07-01", "reasoning": "x"}}
    )
    payload = json.loads(out)
    assert payload["answers"] == {"city": "Pune"}
    assert payload["skipped"] == []
    assert payload["recent_turns"][1]["text"] == "hello"
    assert payload["draft_provenance"]["business_type"] == {"source": "gbp", "fetched_at": "2026-07-01"}


def test_loop_exception_returns_none(monkeypatch):
    """A raising model call inside the loop degrades to None (the caller falls back to the walker)."""
    def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _boom)
    assert compose_turn(_STATE, {"website": "https://mysite.in"}, "hi", locale="en", tenant_id="t-1") is None


def test_no_tenant_id_takes_classic_single_call(monkeypatch):
    """Tools-absent turn: with no tenant_id the classic single ``_invoke_llm`` call runs and the tool
    loop is never entered — even when the draft carries a pinnable website. This is what keeps the
    untouched ``test_turn_brain.py`` (which patches ``_invoke_llm``) byte-identically green."""
    used = {"classic": False}

    def _classic(system, user):
        used["classic"] = True
        return json.dumps(_FINAL)

    def _tools(*a, **k):
        raise AssertionError("the tool loop must not run without a tenant_id")

    monkeypatch.setattr(turn_brain, "_invoke_llm", _classic)
    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _tools)
    plan = compose_turn(_STATE, {"website": "https://mysite.in"}, "hi", locale="en")  # no tenant_id
    assert isinstance(plan, TurnPlan)
    assert used["classic"]


def test_pinnable_domains_from_website_and_message():
    """Pinnable hosts come from the draft website + any dotted host in the owner's message; plain chat
    with no domain yields none (so a normal turn stays fast)."""
    assert turn_brain._pinnable_domains({"website": "https://mysite.in/about"}, "check rkecom.in too") == [
        "mysite.in", "rkecom.in",
    ]
    assert turn_brain._pinnable_domains({}, "we're open 9am-9pm in Pune") == []


# --- VT-662: empty-betas header regression (the turn-brain was silently dead on dev) ----------------


def test_invoke_llm_tools_omits_empty_betas_header(monkeypatch):
    """VT-662 — ``betas=[]`` must NOT be forwarded to ``beta.messages.create``. An empty list makes the
    SDK emit an ``anthropic-beta:`` header with a blank value → API 400 ("Unexpected value(s) `` for the
    `anthropic-beta` header"), which silently killed the turn-brain on EVERY no-web-fetch onboarding
    turn (→ walker fallback → ignored_speech_act). Non-empty betas MUST still be forwarded."""
    pytest.importorskip("anthropic")  # monkeypatching anthropic.Anthropic requires the module present
    captured: dict[str, Any] = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"reply_text":"hi"}')])

    class _FakeClient:
        beta = SimpleNamespace(messages=_FakeMessages())

    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: _FakeClient())

    turn_brain._invoke_llm_tools("sys", [{"role": "user", "content": "x"}], [], [])
    assert "betas" not in captured, "empty betas must be omitted (blank anthropic-beta header 400s)"

    turn_brain._invoke_llm_tools("sys", [{"role": "user", "content": "x"}], [], ["web-fetch-2025-09-10"])
    assert captured.get("betas") == ["web-fetch-2025-09-10"], "non-empty betas must be forwarded"


# --- Cache batch 2026-07-18: both model seams pass the system as ONE cache_control block ------------


def _assert_cached_system_shape(system: Any, expected_text: str) -> None:
    """The block-list cache shape both seams must emit: ONE text block carrying the full system
    string, marked ephemeral so the per-turn prefix is served from cache."""
    assert isinstance(system, list) and len(system) == 1
    block = system[0]
    assert block["type"] == "text"
    assert block["text"] == expected_text
    assert block["cache_control"] == {"type": "ephemeral"}


def test_invoke_llm_passes_system_as_cache_control_block(monkeypatch):
    """Cache batch — the single-call seam sends ``system`` as a block LIST whose only block carries
    ``cache_control: ephemeral`` and the FULL system string (locale sub included — it is per-owner
    stable and belongs inside the cached prefix). Volatile content stays on the user prompt."""
    pytest.importorskip("anthropic")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"reply_text":"hi"}')])

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: _FakeClient())

    turn_brain._invoke_llm("SYSTEM en-locale", "USER volatile")
    _assert_cached_system_shape(captured["system"], "SYSTEM en-locale")
    assert captured["messages"] == [{"role": "user", "content": "USER volatile"}]


def test_invoke_llm_tools_passes_system_as_cache_control_block(monkeypatch):
    """Cache batch — the tool-loop seam sends the SAME block-list cache shape (the caller-assembled
    system string, _TOOLS_ADDENDUM included, inside the cached block) while the VT-662 empty-betas
    omission stays intact."""
    pytest.importorskip("anthropic")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"reply_text":"hi"}')])

    class _FakeClient:
        beta = SimpleNamespace(messages=_FakeMessages())

    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: _FakeClient())

    turn_brain._invoke_llm_tools(
        "SYSTEM plus addendum", [{"role": "user", "content": "x"}], [], []
    )
    _assert_cached_system_shape(captured["system"], "SYSTEM plus addendum")
    assert "betas" not in captured  # VT-662 guard undisturbed by the cache shape
