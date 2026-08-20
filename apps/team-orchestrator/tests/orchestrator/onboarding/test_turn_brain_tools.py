"""VT-570 — unit tests for the turn-brain TOOL BELT (bounded agentic loop, MOCKED model seam).

No network, no DB: the loop's model-call seam ``_invoke_llm_tools`` is monkeypatched to return canned
response objects, so the loop mechanics (client-tool dispatch → tool result → re-call → final parse),
the iteration cap, host-pinning, and the read_journey_history payload are all exercised deterministically.
The tenant_id gate (present → loop; absent → the classic VT-569 single call, exercised by the untouched
``test_turn_brain.py``) is pinned here too, so the two paths never diverge.

VT-732 — the loop now speaks LANGCHAIN messages through the tier seam instead of raw Anthropic blocks,
so a canned response is an ``AIMessage`` (``.tool_calls`` / ``.content``) and a tool answer is a
``ToolMessage``. The loop's BEHAVIOUR is what these tests pin, and it is unchanged; only the transport
moved. The two ex-SDK regressions (empty betas, the cached system block) are pinned at the seam
boundary now — see the bottom of this file and ``tests/test_llm_structured.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage  # noqa: E402

from orchestrator.onboarding import turn_brain  # noqa: E402
from orchestrator.onboarding.turn_brain import TurnPlan, compose_turn  # noqa: E402


def _tool_call(name: str, tool_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": tool_id, "type": "tool_call"}


def _resp(stop_reason: str, *, text: str = "", tool_calls: list[dict[str, Any]] | None = None) -> Any:
    """A canned model response in the shape the seam returns (langchain ``AIMessage``)."""
    return AIMessage(
        content=text,
        tool_calls=tool_calls or [],
        response_metadata={"stop_reason": stop_reason},
    )


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

    def _fake(system_prompt, messages, tools, betas, native_tools=None):
        calls.append({"messages": list(messages), "tools": tools})
        if len(calls) == 1:
            return _resp("tool_use", tool_calls=[_tool_call("read_journey_history", "tu_1")])
        return _resp("end_turn", text=json.dumps(_FINAL))

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)
    plan = compose_turn(_STATE, {}, "how's it going?", locale="en", tenant_id="t-1")
    assert isinstance(plan, TurnPlan)
    assert plan.reply_text == "All set — thanks!"
    assert len(calls) == 2, "one tool round-trip, then the final call"
    # the read_journey_history result was fed back on the 2nd call and carries the window + answers
    tool_msg = calls[1]["messages"][-1]
    assert tool_msg.type == "tool" and tool_msg.tool_call_id == "tu_1"
    payload = json.loads(tool_msg.content)
    assert payload["answers"] == {"city": "Pune"}
    assert payload["recent_turns"] and payload["recent_turns"][0]["text"] == "hi"


def test_iteration_cap_forces_finalization(monkeypatch):
    """A model that keeps requesting tools is capped: after ``_MAX_TOOL_ITERS`` round-trips the loop
    forces a final NO-TOOLS call (``tools == []``) and parses that answer."""
    calls: list[list[Any]] = []

    def _fake(system_prompt, messages, tools, betas, native_tools=None):
        calls.append(tools)
        if tools:  # still offering tools → the model keeps requesting one
            return _resp("tool_use", tool_calls=[_tool_call("read_journey_history", f"tu_{len(calls)}")])
        return _resp("end_turn", text=json.dumps(_FINAL))  # forced final (tools == [])

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)
    plan = compose_turn(_STATE, {}, "hi", locale="en", tenant_id="t-1")
    assert isinstance(plan, TurnPlan)
    assert calls[-1] == [], "the final call is forced with no tools"
    # initial call + _MAX_TOOL_ITERS round-trips + 1 forced final
    assert len(calls) == turn_brain._MAX_TOOL_ITERS + 2


def test_immediate_final_no_tool_call(monkeypatch):
    """Most turns need no tool: a first response with no tool_use parses straight through (one call)."""
    calls: list[Any] = []

    def _fake(system_prompt, messages, tools, betas, native_tools=None):
        calls.append(tools)
        return _resp("end_turn", text=json.dumps(_FINAL))

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)
    plan = compose_turn(_STATE, {}, "just chatting", locale="en", tenant_id="t-1")
    assert isinstance(plan, TurnPlan) and plan.reply_text == "All set — thanks!"
    assert len(calls) == 1


def test_web_fetch_and_refresh_offered_only_when_domain_pinnable(monkeypatch):
    """web_fetch + refresh_discovery are offered only when the owner's own domains are pinnable (draft
    website or a URL in the message); read_journey_history is ALWAYS on.

    VT-732 — web_fetch is an Anthropic SERVER-side builtin, so it rides ``native_tools["anthropic"]``
    (bound only when the tier resolves to anthropic) while the client tools stay portable."""
    captured: dict[str, Any] = {}

    def _fake(system_prompt, messages, tools, betas, native_tools=None):
        captured["names"] = [t.get("type") or t.get("name") for t in tools]
        captured["native"] = [
            t.get("type") or t.get("name")
            for specs in (native_tools or {}).values()
            for t in specs
        ]
        captured["betas"] = list(betas)
        return _resp("end_turn", text=json.dumps(_FINAL))

    monkeypatch.setattr(turn_brain, "_invoke_llm_tools", _fake)

    compose_turn(_STATE, {"website": "https://mysite.in"}, "hi", locale="en", tenant_id="t-1")
    assert any("web_fetch" in str(n) for n in captured["native"]), "web_fetch when a domain is pinnable"
    assert captured["betas"] == [turn_brain._WEB_FETCH_BETA]
    assert "refresh_discovery" in captured["names"]
    assert "read_journey_history" in captured["names"]

    compose_turn(_STATE, {}, "hi", locale="en", tenant_id="t-1")  # no website, no URL in body
    assert captured["native"] == [], "no web_fetch without a pinnable domain"
    assert captured["betas"] == [], "no web-fetch beta either (VT-662: empty stays empty)"
    assert "refresh_discovery" not in captured["names"]
    assert "read_journey_history" in captured["names"], "read_journey_history is always on"


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


def test_invoke_llm_tools_omits_empty_betas(monkeypatch):
    """VT-662 — ``betas=[]`` must NOT reach the client. An empty list makes the SDK emit an
    ``anthropic-beta:`` header with a blank value → API 400 ("Unexpected value(s) `` for the
    `anthropic-beta` header"), which silently killed the turn-brain on EVERY no-web-fetch onboarding
    turn (→ walker fallback → ignored_speech_act). Non-empty betas MUST still be forwarded.

    VT-732 — the omission now lives in the seam (``messages_call`` passes ``betas or None``, and
    ``resolve_chat_model`` only sets the ctor field when non-empty); this pins the turn-brain's half
    of that contract, ``tests/test_llm_structured.py`` pins the seam's."""
    captured: dict[str, Any] = {}

    def _fake_messages_call(tier, **kwargs):
        captured.clear()
        captured["tier"] = tier
        captured.update(kwargs)
        return _resp("end_turn", text='{"reply_text":"hi"}')

    monkeypatch.setattr("orchestrator.llm.structured.messages_call", _fake_messages_call)

    turn_brain._invoke_llm_tools("sys", ["x"], [], [])
    assert captured["betas"] is None, "empty betas must not reach the client"

    turn_brain._invoke_llm_tools("sys", ["x"], [], ["web-fetch-2025-09-10"])
    assert captured["betas"] == ["web-fetch-2025-09-10"], "non-empty betas must be forwarded"


# --- Cache batch 2026-07-18: both model seams ask the seam for the cached system block --------------


def test_invoke_llm_asks_for_cached_system_on_the_conversational_tier(monkeypatch):
    """Cache batch — the single-call seam sends the FULL system string (locale sub included: it is
    per-owner stable and belongs inside the cached prefix) with ``cache_system=True``, on the
    env-resolved conversational TIER rather than a hardcoded model. Volatile content stays on the
    user prompt. The block shape itself is pinned in tests/test_llm_structured.py."""
    captured: dict[str, Any] = {}

    def _fake_text_call(tier, **kwargs):
        captured["tier"] = tier
        captured.update(kwargs)
        return '{"reply_text":"hi"}'

    monkeypatch.setattr("orchestrator.llm.structured.structured_text_call", _fake_text_call)

    turn_brain._invoke_llm("SYSTEM en-locale", "USER volatile")
    assert captured["tier"] == turn_brain._TURN_TIER == "complex"
    assert captured["system"] == "SYSTEM en-locale"
    assert captured["user"] == "USER volatile"
    assert captured["cache_system"] is True
    assert captured["timeout_s"] == turn_brain._TURN_TIMEOUT_S


def test_invoke_llm_tools_asks_for_cached_system_on_the_conversational_tier(monkeypatch):
    """Cache batch — the tool-loop seam makes the SAME request (caller-assembled system string with
    _TOOLS_ADDENDUM already appended, cached, same tier) while the VT-662 empty-betas omission holds."""
    captured: dict[str, Any] = {}

    def _fake_messages_call(tier, **kwargs):
        captured["tier"] = tier
        captured.update(kwargs)
        return _resp("end_turn", text='{"reply_text":"hi"}')

    monkeypatch.setattr("orchestrator.llm.structured.messages_call", _fake_messages_call)

    turn_brain._invoke_llm_tools("SYSTEM plus addendum", ["x"], [], [])
    assert captured["tier"] == turn_brain._TURN_TIER == "complex"
    assert captured["system"] == "SYSTEM plus addendum"
    assert captured["cache_system"] is True
    assert captured["betas"] is None  # VT-662 guard undisturbed by the cache shape
