"""Migration-176 — web / X-search CAPABILITY across the multi-provider seam.

Two halves:
  * BINDING — ``resolve_chat_model(enable_web_search=/enable_x_search=)`` binds each provider's NATIVE
    server-side search tool in the EXACT langchain form verified against the installed pins, gated by
    the master ``TEAM_ENABLE_WEB_SEARCH`` kill switch. x_search is xAI-only; GLM has no server search.
  * COST — ``LlmUsageCallback`` pulls the per-provider server-side search count off the response and
    threads ``search_count`` + ``search_cost_usd`` into ``record_llm_call`` (fail-soft, default 0/0).

Pure unit test: langchain is importorskip'd (dep-less smoke skips cleanly); ctors get dummy keys and
are never called over the network; the ledger + search pricing are monkeypatched.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langchain_openai")
pytest.importorskip("langchain_google_genai")

from orchestrator.llm import ledger as ledger_mod  # noqa: E402
from orchestrator.llm import pricing as pricing_mod  # noqa: E402
from orchestrator.llm import provider as p  # noqa: E402
from orchestrator.llm.usage_callback import LlmUsageCallback  # noqa: E402


@pytest.fixture(autouse=True)
def _search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy provider keys so the ctors build, master search flag ON, and TEAM_MODEL_* cleared so
    each test picks the model via TEAM_MODEL_SPECIALIST. Also stub search pricing to the seed mirror
    so no live DB read is attempted in the cost tests."""
    for var in (
        "TEAM_MODEL_ROUTINE",
        "TEAM_MODEL_COMPLEX",
        "TEAM_MODEL_CLASSIFIER",
        "TEAM_MODEL_SPECIALIST",
        "TEAM_MODEL_REVIEW",
        "TEAM_OPENAI_SERVICE_TIER",
        "TEAM_LLM_BUDGET_ENFORCE",
        "GLM_BASE_URL",
        "XAI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test-not-real")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-not-real")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-real")
    monkeypatch.setenv("TEAM_ENABLE_WEB_SEARCH", "1")
    monkeypatch.setattr(
        pricing_mod, "_search_pricing", lambda: dict(pricing_mod._SEED_SEARCH_PRICING)
    )


def _resolve(monkeypatch, model_id, **kw):
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", model_id)
    return p.resolve_chat_model("specialist", agent="advisor_lane", **kw)


def _bound_tools(model):
    return getattr(model, "kwargs", {}).get("tools")


# --------------------------------------------------------------------------- _search_tools helper
def test_search_tools_specs_per_provider():
    assert p._search_tools("anthropic", web=True, x=False) == [
        {"type": "web_search_20260209", "name": "web_search"}
    ]
    assert p._search_tools("openai", web=True, x=False) == [{"type": "web_search"}]
    assert p._search_tools("xai", web=True, x=True) == [
        {"type": "web_search"},
        {"type": "x_search"},
    ]
    assert p._search_tools("google", web=True, x=False) == [{"google_search": {}}]
    # GLM (zai) has no server web-search on the installed path — empty (skip, no fabricated tool).
    assert p._search_tools("zai", web=True, x=False) == []
    # x_search on a non-xai provider is dropped (X search is xAI-only).
    assert p._search_tools("anthropic", web=False, x=True) == []
    assert p._search_tools("openai", web=False, x=True) == []


# --------------------------------------------------------------------------- master kill switch
def test_master_flag_off_forces_search_off(monkeypatch):
    # TEAM_ENABLE_WEB_SEARCH off => both search kwargs forced off; plain BaseChatModel returned.
    monkeypatch.setenv("TEAM_ENABLE_WEB_SEARCH", "0")
    m = _resolve(monkeypatch, "grok-4.5", enable_web_search=True, enable_x_search=True)
    assert type(m).__name__ == "ChatOpenAI"
    assert _bound_tools(m) is None  # not a bound runnable — no tools


def test_no_search_kwargs_returns_plain_model(monkeypatch):
    m = _resolve(monkeypatch, "claude-sonnet-5")
    assert type(m).__name__ == "ChatAnthropic"


# --------------------------------------------------------------------------- binding form per provider
def test_anthropic_web_search_binds_builtin_dict(monkeypatch):
    m = _resolve(monkeypatch, "claude-sonnet-5", enable_web_search=True)
    # bind_tools passes the web_search_* builtin dict through untouched.
    assert _bound_tools(m) == [{"type": "web_search_20260209", "name": "web_search"}]


def test_openai_web_search_binds_raw_type_dict(monkeypatch):
    m = _resolve(monkeypatch, "gpt-5.6-terra", enable_web_search=True)
    assert _bound_tools(m) == [{"type": "web_search"}]


def test_xai_web_and_x_search_bind_raw(monkeypatch):
    # Grok binds BOTH web + x search via .bind(tools=...) (NOT bind_tools, which would mangle
    # x_search — see the provider docstring). The raw specs survive unchanged.
    m = _resolve(monkeypatch, "grok-4.5", enable_web_search=True, enable_x_search=True)
    assert _bound_tools(m) == [{"type": "web_search"}, {"type": "x_search"}]


def test_xai_x_search_only(monkeypatch):
    m = _resolve(monkeypatch, "grok-4.3", enable_x_search=True)
    assert _bound_tools(m) == [{"type": "x_search"}]


def test_google_web_search_binds_grounding(monkeypatch):
    # bind_tools([{"google_search": {}}]) converts to the genai grounding Tool — assert it bound a
    # non-empty tools list with google_search populated (and did not raise).
    m = _resolve(monkeypatch, "gemini-3.5-flash", enable_web_search=True)
    tools = _bound_tools(m)
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0].get("google_search") is not None


def test_x_search_on_non_xai_is_ignored(monkeypatch):
    # x_search requested on anthropic => dropped; a plain (unbound) model is returned.
    m = _resolve(monkeypatch, "claude-sonnet-5", enable_x_search=True)
    assert type(m).__name__ == "ChatAnthropic"
    assert _bound_tools(m) is None


def test_glm_web_search_skipped_model_still_usable(monkeypatch):
    # GLM has no server web-search on the installed path — resolve still returns a usable model,
    # just without a bound search tool.
    m = _resolve(monkeypatch, "glm-5.2", enable_web_search=True)
    assert type(m).__name__ == "ChatOpenAI"
    assert _bound_tools(m) is None


# --------------------------------------------------------------------------- search-cost extraction
def _capture(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ledger_mod, "record_llm_call", lambda **k: calls.append(k))
    return calls


def _msg(*, model, content=None, response_metadata=None):
    rmeta = {"model_name": model}
    if response_metadata:
        rmeta.update(response_metadata)
    return SimpleNamespace(
        usage_metadata={"input_tokens": 10, "output_tokens": 5},
        response_metadata=rmeta,
        content=content,
        id=None,
    )


def _resp(msg, llm_output=None):
    return SimpleNamespace(
        generations=[[SimpleNamespace(message=msg, text="")]], llm_output=llm_output or {}
    )


def _record(monkeypatch, response):
    calls = _capture(monkeypatch)
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="advisor_lane", call_site="specialist")
    cb.on_llm_end(response)
    assert len(calls) == 1
    return calls[0]


def test_anthropic_server_tool_use_count(monkeypatch):
    # Anthropic surfaces the count at llm_output.usage.server_tool_use.web_search_requests.
    resp = _resp(
        _msg(model="claude-sonnet-5"),
        llm_output={"usage": {"server_tool_use": {"web_search_requests": 2}}},
    )
    c = _record(monkeypatch, resp)
    assert c["provider"] == "anthropic"
    assert c["search_count"] == 2
    assert c["search_cost_usd"] == Decimal("0.02")  # $10/1000 * 2


def test_xai_responses_search_call_blocks(monkeypatch):
    # xAI Responses surfaces search invocations as *_search_call content blocks.
    content = [
        {"type": "text", "text": "hi"},
        {"type": "web_search_call"},
        {"type": "web_search_call"},
        {"type": "x_search_call"},
    ]
    c = _record(monkeypatch, _resp(_msg(model="grok-4.5", content=content)))
    assert c["provider"] == "xai"
    assert c["search_count"] == 3
    # 2 web ($5/1000) + 1 x ($5/1000) = 0.010 + 0.005 = 0.015.
    assert c["search_cost_usd"] == Decimal("0.015")


def test_openai_responses_web_search_call_block(monkeypatch):
    content = [{"type": "web_search_call"}]
    c = _record(monkeypatch, _resp(_msg(model="gpt-5.6-sol", content=content)))
    assert c["provider"] == "openai"
    assert c["search_count"] == 1
    assert c["search_cost_usd"] == Decimal("0.01")  # placeholder $10/1000


def test_google_grounding_counts_one_request(monkeypatch):
    # Google grounding is billed per grounded REQUEST, not per query → count 1.
    msg = _msg(
        model="gemini-3.5-flash",
        response_metadata={"grounding_metadata": {"web_search_queries": ["q1", "q2"]}},
    )
    c = _record(monkeypatch, _resp(msg))
    assert c["provider"] == "google"
    assert c["search_count"] == 1
    assert c["search_cost_usd"] == Decimal("0.035")  # placeholder $35/1000


def test_no_search_records_zero(monkeypatch):
    # A plain call (no search surface) records 0/0 — the ledger column defaults, unchanged behavior.
    c = _record(monkeypatch, _resp(_msg(model="claude-sonnet-5", content="plain text")))
    assert c["search_count"] == 0
    assert c["search_cost_usd"] == 0


def test_search_extraction_never_raises(monkeypatch):
    # A malformed response must not break metering — search extraction fails soft to 0/0.
    calls = _capture(monkeypatch)
    weird = SimpleNamespace(generations=[[SimpleNamespace(message=object(), text="")]], llm_output=None)
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="advisor_lane", call_site="specialist")
    cb.on_llm_end(weird)  # no raise
    assert calls and calls[0]["search_count"] == 0
