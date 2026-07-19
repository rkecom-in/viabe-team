"""Migration-173 langchain usage callback: usage extraction → ledger.

Requires langchain_core (the BaseCallbackHandler base) — importorskip so the
dep-less smoke skips this cleanly. ``record_llm_call`` is monkeypatched to capture
the args the callback forwards.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")

from orchestrator.llm import ledger as ledger_mod  # noqa: E402
from orchestrator.llm.usage_callback import LlmUsageCallback  # noqa: E402


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(ledger_mod, "record_llm_call", lambda **k: calls.append(k))
    return calls


def _chat_response(*, input_tokens, output_tokens, model, msg_id=None):
    """LLMResult with usage on the message (ChatAnthropic / chat-wrapper shape)."""
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens},
        response_metadata={"model_name": model, "id": msg_id} if msg_id else {"model_name": model},
        id=msg_id,
    )
    gen = SimpleNamespace(message=msg, text="")
    return SimpleNamespace(generations=[[gen]], llm_output={})


def test_extracts_usage_and_records(monkeypatch):
    calls = _capture(monkeypatch)
    tid = uuid4()
    cb = LlmUsageCallback(tenant_id=tid, agent="team_manager", call_site="dispatch_brain")
    cb.on_llm_end(_chat_response(input_tokens=120, output_tokens=34, model="claude-sonnet-5", msg_id="msg_x"))

    assert len(calls) == 1
    c = calls[0]
    assert c["tenant_id"] == tid
    assert c["agent"] == "team_manager"
    assert c["call_site"] == "dispatch_brain"
    assert c["tokens_in"] == 120
    assert c["tokens_out"] == 34
    assert c["model"] == "claude-sonnet-5"
    assert c["provider"] == "anthropic"
    assert c["request_id"] == "msg_x"


def test_cache_read_tokens_split_out(monkeypatch):
    # langchain's input_tokens INCLUDES cache reads; input_token_details.cache_read
    # is the subset. Callback returns full-price = total - cache_read + cached = read.
    calls = _capture(monkeypatch)
    msg = SimpleNamespace(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "input_token_details": {"cache_read": 40},
        },
        response_metadata={"model_name": "claude-sonnet-5", "id": "msg_c"},
        id="msg_c",
    )
    response = SimpleNamespace(generations=[[SimpleNamespace(message=msg, text="")]], llm_output={})
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="team_manager", call_site="dispatch_brain")
    cb.on_llm_end(response)
    assert calls[0]["tokens_in"] == 60  # 100 total - 40 cache_read
    assert calls[0]["cached_tokens_in"] == 40
    assert calls[0]["tokens_out"] == 20


def test_no_cache_detail_means_zero_cached(monkeypatch):
    calls = _capture(monkeypatch)
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="team_manager", call_site="dispatch_brain")
    cb.on_llm_end(_chat_response(input_tokens=80, output_tokens=10, model="claude-sonnet-5"))
    assert calls[0]["tokens_in"] == 80
    assert calls[0]["cached_tokens_in"] == 0


def test_openai_provider_inferred_from_model_prefix(monkeypatch):
    calls = _capture(monkeypatch)
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="team_manager", call_site="dispatch_brain")
    cb.on_llm_end(_chat_response(input_tokens=10, output_tokens=5, model="gpt-5.6-sol"))
    assert calls[0]["provider"] == "openai"
    assert calls[0]["model"] == "gpt-5.6-sol"


def test_llm_output_fallback_surface(monkeypatch):
    """Older/other providers land usage in llm_output.token_usage, not usage_metadata."""
    calls = _capture(monkeypatch)
    response = SimpleNamespace(
        generations=[[SimpleNamespace(message=None, text="")]],
        llm_output={
            "model_name": "claude-haiku-4-5",
            "token_usage": {"input_tokens": 77, "output_tokens": 11},
        },
    )
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="sales_recovery", call_site="sr")
    cb.on_llm_end(response)
    assert calls[0]["tokens_in"] == 77
    assert calls[0]["tokens_out"] == 11
    assert calls[0]["model"] == "claude-haiku-4-5"


def test_missing_usage_records_zeros_and_unknown_model(monkeypatch):
    calls = _capture(monkeypatch)
    response = SimpleNamespace(generations=None, llm_output=None)
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="team_manager", call_site="dispatch_brain")
    cb.on_llm_end(response)
    assert calls[0]["tokens_in"] == 0
    assert calls[0]["tokens_out"] == 0
    assert calls[0]["model"] == "unknown"
    assert calls[0]["provider"] == "anthropic"


def test_callback_never_raises_when_ledger_errors(monkeypatch):
    def _boom(**k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(ledger_mod, "record_llm_call", _boom)
    cb = LlmUsageCallback(tenant_id=uuid4(), agent="team_manager", call_site="dispatch_brain")
    # Must swallow — the callback is a pure observer on the model call.
    cb.on_llm_end(_chat_response(input_tokens=1, output_tokens=1, model="claude-sonnet-5"))


def test_constructor_signature_is_positional_tenant_agent_callsite():
    # The provider seam constructs this positionally — lock the signature.
    cb = LlmUsageCallback(uuid4(), "team_manager", "dispatch_brain")
    assert cb.agent == "team_manager"
    assert cb.call_site == "dispatch_brain"
