"""VT-732 — unit tests for the seam extensions that let the direct-SDK sites be ported without loss.

The bypassing call sites (turn_brain, the onboarding classifiers, the manager review/verification,
the vision + tool-loop paths) each needed something the v1 seam did not carry: a request timeout, an
Anthropic prompt-cache block, a last-text-block parse, tool loops, or beta headers. These tests pin
each of those so a future "simplification" cannot quietly turn the port into a downgrade.

Placed at the tests/ top level (not tests/orchestrator/) so the package autouse DB/twilio fixtures do
not apply — pure unit tests, no network: every model is built with dummy keys and invoked through a
stub, never over the wire.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langchain_openai")
pytest.importorskip("langchain_google_genai")

from orchestrator.llm import provider as p  # noqa: E402
from orchestrator.llm import structured as s  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TEAM_MODEL_ROUTINE",
        "TEAM_MODEL_COMPLEX",
        "TEAM_MODEL_CLASSIFIER",
        "TEAM_MODEL_SPECIALIST",
        "TEAM_MODEL_REVIEW",
        "TEAM_OPENAI_SERVICE_TIER",
        "TEAM_LLM_BUDGET_ENFORCE",
        "TEAM_ENABLE_WEB_SEARCH",
        "GLM_BASE_URL",
        "XAI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test-not-real")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-not-real")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-real")


class _StubResponse:
    def __init__(self, content: Any, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _StubModel:
    """Records what the seam bound + what it was invoked with; returns a canned response."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.invoked_with: Any = None
        self.bound_tools: Any = None
        self.bind_style: str | None = None

    def bind_tools(self, tools: Any) -> _StubModel:
        self.bound_tools = tools
        self.bind_style = "bind_tools"
        return self

    def bind(self, **kwargs: Any) -> _StubModel:
        self.bound_tools = kwargs.get("tools")
        self.bind_style = "bind"
        return self

    def invoke(self, messages: Any) -> Any:
        self.invoked_with = messages
        return self.response


def _patch_model(monkeypatch: pytest.MonkeyPatch, stub: _StubModel) -> dict[str, Any]:
    """Replace resolve_chat_model in the structured module and capture its kwargs."""
    seen: dict[str, Any] = {}

    def _fake(tier: str, **kwargs: Any) -> _StubModel:
        seen["tier"] = tier
        seen.update(kwargs)
        return stub

    monkeypatch.setattr(p, "resolve_chat_model", _fake)
    return seen


# --------------------------------------------------------------------------- timeouts
def test_timeout_maps_to_anthropic_default_request_timeout() -> None:
    m = p.resolve_chat_model("complex", agent="t", timeout_s=10.0)
    assert type(m).__name__ == "ChatAnthropic"
    assert m.default_request_timeout == 10.0


def test_timeout_maps_to_openai_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    m = p.resolve_chat_model("complex", agent="t", timeout_s=12.0)
    assert m.request_timeout == 12.0


def test_explicit_timeout_wins_over_flex_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 10s hot-path budget must not become a 15-min flex wait — the owner's turn is what stalls."""
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("complex", agent="t", timeout_s=10.0)
    assert m.service_tier == "flex"
    assert m.request_timeout == 10.0


def test_flex_ceiling_still_applies_without_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    assert p.resolve_chat_model("complex", agent="t").request_timeout == p._FLEX_TIMEOUT_S


def test_timeout_maps_to_google_and_glm_and_grok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gemini-3.5-flash")
    assert p.resolve_chat_model("complex", agent="t", timeout_s=9.0).timeout == 9.0
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "glm-5.2")
    assert p.resolve_chat_model("complex", agent="t", timeout_s=9.0).request_timeout == 9.0
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "grok-4.5")
    assert p.resolve_chat_model("complex", agent="t", timeout_s=9.0).request_timeout == 9.0


def test_no_timeout_leaves_client_default() -> None:
    assert p.resolve_chat_model("complex", agent="t").default_request_timeout is None


# --------------------------------------------------------------------------- reasoning-cap floor
def test_small_cap_is_floored_on_the_responses_api_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 60-token cap sized for Anthropic returns NO TEXT on a reasoning model — the cap covers
    reasoning there. Observed on dev as an empty triage response that cost the turn its SR
    delegation, so the seam raises it rather than letting each site fail soft into a fallback."""
    monkeypatch.setenv("TEAM_MODEL_CLASSIFIER", "gpt-5.6-luna")
    assert p.resolve_chat_model("classifier", agent="t", max_tokens=60).max_tokens == (
        p._REASONING_MIN_MAX_TOKENS
    )
    monkeypatch.setenv("TEAM_MODEL_CLASSIFIER", "grok-4.5")
    assert p.resolve_chat_model("classifier", agent="t", max_tokens=10).max_tokens == (
        p._REASONING_MIN_MAX_TOKENS
    )


def test_generous_cap_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_CLASSIFIER", "gpt-5.6-luna")
    assert p.resolve_chat_model("classifier", agent="t", max_tokens=4096).max_tokens == 4096


def test_anthropic_and_google_and_glm_keep_the_callers_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the Responses-API providers spend the cap on reasoning; everywhere else the caller's
    hand-tuned number is the contract (a 16-token taxonomy key must stay 16 tokens)."""
    assert p.resolve_chat_model("classifier", agent="t", max_tokens=16).max_tokens == 16
    monkeypatch.setenv("TEAM_MODEL_CLASSIFIER", "gemini-3.5-flash")
    assert p.resolve_chat_model("classifier", agent="t", max_tokens=16).max_output_tokens == 16
    monkeypatch.setenv("TEAM_MODEL_CLASSIFIER", "glm-5.2")
    assert p.resolve_chat_model("classifier", agent="t", max_tokens=16).max_tokens == 16


def test_empty_response_error_names_the_model_and_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure that cost a dev run its delegation said only 'empty response from triage'."""
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    stub = _StubModel(_StubResponse(""))
    stub.response.response_metadata = {"finish_reason": "length"}
    _patch_model(monkeypatch, stub)
    with pytest.raises(ValueError, match="gpt-5.6-luna.*length"):
        s.structured_text_call("complex", user="u", max_tokens=200, agent="a", call_site="triage")


# --------------------------------------------------------------------------- betas
def test_betas_passed_on_anthropic() -> None:
    m = p.resolve_chat_model("complex", agent="t", betas=["web-fetch-2025-09-10"])
    assert m.betas == ["web-fetch-2025-09-10"]


def test_empty_betas_never_reach_the_client() -> None:
    """VT-662: an empty list emits a blank anthropic-beta header and the API 400s it."""
    assert p.resolve_chat_model("complex", agent="t", betas=[]).betas is None


def test_betas_dropped_on_non_anthropic_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    m = p.resolve_chat_model("complex", agent="t", betas=["web-fetch-2025-09-10"])
    assert type(m).__name__ == "ChatOpenAI"  # built, not refused


# --------------------------------------------------------------------------- structured_text_call
def test_system_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubModel(_StubResponse("hi"))
    _patch_model(monkeypatch, stub)
    assert s.structured_text_call("classifier", user="u", max_tokens=10, agent="a", call_site="c") == "hi"
    assert len(stub.invoked_with) == 1  # user only — no empty system turn


def test_cache_system_is_a_block_on_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubModel(_StubResponse("ok"))
    _patch_model(monkeypatch, stub)
    s.structured_text_call(
        "complex", system="SYS", user="u", max_tokens=10, agent="a", call_site="c", cache_system=True
    )
    system_turn = stub.invoked_with[0]
    assert system_turn.content == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]


def test_cache_system_is_plain_text_off_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other providers reject the block; the TEXT stays identical, only the caching is lost."""
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    stub = _StubModel(_StubResponse("ok"))
    _patch_model(monkeypatch, stub)
    s.structured_text_call(
        "complex", system="SYS", user="u", max_tokens=10, agent="a", call_site="c", cache_system=True
    )
    assert stub.invoked_with[0].content == "SYS"


def test_text_mode_last_skips_search_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web-search sites parse the LAST text block — joining feeds 'I'll search…' into json.loads."""
    stub = _StubModel(
        _StubResponse(
            [
                {"type": "text", "text": "I'll look that up."},
                {"type": "server_tool_use", "name": "web_search"},
                {"type": "text", "text": '{"ok": true}'},
            ]
        )
    )
    _patch_model(monkeypatch, stub)
    out = s.structured_text_call(
        "complex", user="u", max_tokens=10, agent="a", call_site="c", text_mode="last"
    )
    assert out == '{"ok": true}'


def test_text_mode_join_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubModel(_StubResponse([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
    _patch_model(monkeypatch, stub)
    out = s.structured_text_call("complex", user="u", max_tokens=10, agent="a", call_site="c")
    assert out == "ab"


def test_empty_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, _StubModel(_StubResponse("   ")))
    with pytest.raises(ValueError):
        s.structured_text_call("complex", user="u", max_tokens=10, agent="a", call_site="c")


def test_timeout_and_search_thread_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_model(monkeypatch, _StubModel(_StubResponse("ok")))
    s.structured_text_call(
        "complex", user="u", max_tokens=10, agent="a", call_site="c",
        timeout_s=7.0, enable_web_search=True,
    )
    assert seen["timeout_s"] == 7.0
    assert seen["enable_web_search"] is True


# --------------------------------------------------------------------------- messages_call
def test_messages_call_returns_raw_response(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _StubResponse("text", tool_calls=[{"name": "t", "args": {}, "id": "1"}])
    _patch_model(monkeypatch, _StubModel(resp))
    out = s.messages_call("complex", messages=["m"], max_tokens=10, agent="a", call_site="c")
    assert out is resp
    assert out.tool_calls[0]["name"] == "t"


def test_messages_call_binds_portable_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubModel(_StubResponse("ok"))
    _patch_model(monkeypatch, stub)
    tool = {"name": "read_journey_history", "description": "d", "input_schema": {"type": "object"}}
    s.messages_call("complex", messages=["m"], max_tokens=10, agent="a", call_site="c", tools=[tool])
    assert stub.bind_style == "bind_tools"
    assert stub.bound_tools == [tool]


def test_messages_call_native_tools_bind_on_matching_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubModel(_StubResponse("ok"))
    _patch_model(monkeypatch, stub)
    native = {"type": "web_fetch_20250910", "name": "web_fetch"}
    s.messages_call(
        "complex", messages=["m"], max_tokens=10, agent="a", call_site="c",
        native_tools={"anthropic": [native]},
    )
    assert stub.bound_tools == [native]


def test_messages_call_native_tools_dropped_off_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Anthropic builtin bound to an OpenAI model is a 400 — drop it and keep the call alive."""
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    stub = _StubModel(_StubResponse("ok"))
    _patch_model(monkeypatch, stub)
    tool = {"name": "read_journey_history", "description": "d", "input_schema": {"type": "object"}}
    s.messages_call(
        "complex", messages=["m"], max_tokens=10, agent="a", call_site="c",
        tools=[tool], native_tools={"anthropic": [{"type": "web_fetch_20250910", "name": "web_fetch"}]},
    )
    assert stub.bind_style == "bind"  # ONE .bind for the ChatOpenAI-based providers
    assert [t["function"]["name"] for t in stub.bound_tools] == ["read_journey_history"]


def test_messages_call_no_tools_leaves_model_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubModel(_StubResponse("ok"))
    _patch_model(monkeypatch, stub)
    s.messages_call("complex", messages=["m"], max_tokens=10, agent="a", call_site="c")
    assert stub.bind_style is None


def test_messages_call_betas_only_on_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_model(monkeypatch, _StubModel(_StubResponse("ok")))
    s.messages_call(
        "complex", messages=["m"], max_tokens=10, agent="a", call_site="c", betas=["web-fetch-2025-09-10"]
    )
    assert seen["betas"] == ["web-fetch-2025-09-10"]

    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-5.6-luna")
    seen2 = _patch_model(monkeypatch, _StubModel(_StubResponse("ok")))
    s.messages_call(
        "complex", messages=["m"], max_tokens=10, agent="a", call_site="c", betas=["web-fetch-2025-09-10"]
    )
    assert seen2["betas"] is None


def test_response_text_modes() -> None:
    resp = _StubResponse([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert s.response_text(resp) == "ab"
    assert s.response_text(resp, mode="last") == "b"
