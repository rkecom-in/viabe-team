"""VT-619b — unit tests for the multi-provider LLM seam (orchestrator.llm.provider).

Covers the registry, the TEAM_MODEL_* env tier mapping (read fresh per call), provider inference,
the loud-failure paths (unknown id / gpt at an Anthropic-only site / unknown tier), the ChatOpenAI
Responses-API + service_tier + flex-timeout wiring, the flex 429 -> auto fallback, the pre-call
budget hook (default off), and the fail-soft usage-recording callback.

Placed at the tests/ top level (not tests/orchestrator/) so the package autouse DB/twilio fixtures
do not apply — this is a pure unit test. The OpenAI client is never called over the network: ctor
tests inject a dummy OPENAI_API_KEY and the flex-fallback test mocks the rebuild + invoke.
"""

from __future__ import annotations

import sys
import types

import pytest

# The seam is built on langchain — SKIP cleanly in the dep-less smoke env.
pytest.importorskip("langchain_core")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langchain_openai")

from orchestrator.llm import provider as p  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee unset TEAM_MODEL_* / service-tier / budget env so tests see the built-in defaults,
    and a dummy OPENAI_API_KEY so ChatOpenAI ctors never fail on a missing credential."""
    for var in (
        "TEAM_MODEL_ROUTINE",
        "TEAM_MODEL_COMPLEX",
        "TEAM_MODEL_CLASSIFIER",
        "TEAM_MODEL_SPECIALIST",
        "TEAM_MODEL_REVIEW",
        "TEAM_OPENAI_SERVICE_TIER",
        "TEAM_LLM_BUDGET_ENFORCE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")


# --------------------------------------------------------------------------- registry
def test_registry_has_exactly_six_supported() -> None:
    assert p.SUPPORTED_MODELS == p.ANTHROPIC_MODELS | p.OPENAI_MODELS
    assert len(p.SUPPORTED_MODELS) == 6
    assert p.ANTHROPIC_MODELS == {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}
    assert p.OPENAI_MODELS == {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}


# --------------------------------------------------------------------------- provider inference
def test_provider_inference_by_prefix() -> None:
    assert p.provider_for("claude-sonnet-5") == "anthropic"
    assert p.provider_for("gpt-5.6-terra") == "openai"


def test_provider_inference_unknown_prefix_raises() -> None:
    with pytest.raises(p.UnknownModelError):
        p.provider_for("gemini-2.0-pro")


# --------------------------------------------------------------------------- env tier mapping
def test_tier_defaults() -> None:
    assert p.resolve_model_id("routine") == "claude-haiku-4-5"
    assert p.resolve_model_id("complex") == "claude-sonnet-5"
    assert p.resolve_model_id("classifier") == "claude-haiku-4-5"
    assert p.resolve_model_id("specialist") == "claude-sonnet-5"
    assert p.resolve_model_id("review") == "claude-opus-4-8"


def test_tier_env_override_read_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # No import-time freeze — a mid-process env change is picked up on the next call.
    assert p.resolve_model_id("specialist") == "claude-sonnet-5"
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gpt-5.6-terra")
    assert p.resolve_model_id("specialist") == "gpt-5.6-terra"


def test_unknown_tier_raises() -> None:
    with pytest.raises(ValueError):
        p.resolve_model_id("nonsense-tier")


# --------------------------------------------------------------------------- loud failures
def test_unknown_model_id_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-4o")  # a real model, but NOT one of the six
    with pytest.raises(p.UnknownModelError) as exc:
        p.resolve_model_id("complex")
    # the error names the supported set
    assert "gpt-5.6-terra" in str(exc.value)


def test_require_anthropic_model_rejects_gpt() -> None:
    with pytest.raises(p.UnknownModelError) as exc:
        p.require_anthropic_model("gpt-5.6-luna", site="triage")
    assert "triage" in str(exc.value)


def test_require_anthropic_model_passes_claude() -> None:
    assert p.require_anthropic_model("claude-sonnet-5", site="triage") == "claude-sonnet-5"


# --------------------------------------------------------------------------- model construction
def test_resolve_chat_model_anthropic_default() -> None:
    m = p.resolve_chat_model("complex", agent="team_manager")
    assert type(m).__name__ == "ChatAnthropic"
    assert m.model == "claude-sonnet-5"


def test_resolve_chat_model_openai_responses_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gpt-5.6-terra")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    assert type(m).__name__ == "ChatOpenAI"
    assert m.model_name == "gpt-5.6-terra"
    assert m.use_responses_api is True
    assert m.max_retries == p._OPENAI_MAX_RETRIES
    # standard tier -> service_tier omitted (None); no flex timeout widening.
    assert m.service_tier is None
    assert m.request_timeout is None


def test_resolve_chat_model_openai_flex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gpt-5.6-sol")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    assert m.service_tier == "flex"
    assert m.request_timeout == p._FLEX_TIMEOUT_S


def test_invalid_service_tier_falls_back_to_standard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "turbo")
    assert p._configured_service_tier() == "standard"


# --------------------------------------------------------------------------- flex fallback
class _FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _FakeModel:
    """A stand-in chat model whose ``invoke`` raises a scripted sequence then returns a sentinel."""

    def __init__(self, script: list[object]) -> None:
        self._script = script
        self.invocations = 0
        self.callbacks: list[object] = []
        self.model_name = "gpt-5.6-terra"
        self.max_tokens = 4096

    def invoke(self, _input: object, **_kwargs: object) -> object:
        self.invocations += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_flex_fallback_retry_succeeds() -> None:
    # 429 once, then success on the backoff-retry — no auto rebuild needed.
    model = _FakeModel([_FakeStatusError(429), "ok-on-retry"])
    out = p.invoke_with_flex_fallback(model, "hi")
    assert out == "ok-on-retry"
    assert model.invocations == 2


def test_flex_fallback_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    # 429 twice -> rebuild at service_tier='auto' and invoke once more (mock the rebuild).
    model = _FakeModel([_FakeStatusError(429), _FakeStatusError(429)])
    auto_model = _FakeModel(["ok-on-auto"])
    monkeypatch.setattr(p, "_rebuild_openai_at_auto", lambda m, cb: auto_model)
    out = p.invoke_with_flex_fallback(model, "hi")
    assert out == "ok-on-auto"
    assert auto_model.invocations == 1


def test_flex_fallback_non_429_propagates() -> None:
    model = _FakeModel([_FakeStatusError(500)])
    with pytest.raises(_FakeStatusError):
        p.invoke_with_flex_fallback(model, "hi")
    assert model.invocations == 1


def test_flex_fallback_success_passthrough() -> None:
    model = _FakeModel(["straight-through"])
    assert p.invoke_with_flex_fallback(model, "hi") == "straight-through"
    assert model.invocations == 1


# --------------------------------------------------------------------------- budget gate
def _install_fake_budget_gate(monkeypatch: pytest.MonkeyPatch, verdict: object) -> None:
    mod = types.ModuleType("orchestrator.llm.budget_gate")
    if isinstance(verdict, Exception):
        def _check(_t: object, _a: object) -> object:
            raise verdict
    else:
        def _check(_t: object, _a: object) -> object:
            return verdict
    mod.check_llm_budget = _check  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "orchestrator.llm.budget_gate", mod)


def test_budget_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even a 'hard' verdict does nothing while enforcement is off (the default).
    _install_fake_budget_gate(monkeypatch, "hard")
    p.enforce_budget("tenant-1", "team_manager")  # no raise


def test_budget_gate_hard_raises_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_LLM_BUDGET_ENFORCE", "1")
    _install_fake_budget_gate(monkeypatch, "hard")
    with pytest.raises(p.BudgetExceededError):
        p.enforce_budget("tenant-1", "team_manager")


def test_budget_gate_soft_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_LLM_BUDGET_ENFORCE", "1")
    _install_fake_budget_gate(monkeypatch, "soft")
    p.enforce_budget("tenant-1", "team_manager")  # no raise


def test_budget_gate_fails_open_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_LLM_BUDGET_ENFORCE", "1")
    _install_fake_budget_gate(monkeypatch, RuntimeError("gate exploded"))
    p.enforce_budget("tenant-1", "team_manager")  # gate error -> fail open, no raise


def test_budget_verdict_shapes() -> None:
    assert p._verdict_is_hard("hard") is True
    assert p._verdict_is_hard({"level": "hard"}) is True
    assert p._verdict_is_hard("soft") is False
    assert p._verdict_is_hard(None) is False

    class _V:
        level = "hard"

    assert p._verdict_is_hard(_V()) is True


# --------------------------------------------------------------------------- seam callbacks
def test_budget_gate_callback_enforces_on_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_LLM_BUDGET_ENFORCE", "1")
    _install_fake_budget_gate(monkeypatch, "hard")
    cb = p._BudgetGateCallback(tenant_id="t", agent="team_manager")
    # on_chat_model_start is the pre-call hook for chat models -> BudgetExceededError propagates.
    with pytest.raises(p.BudgetExceededError):
        cb.on_chat_model_start({}, [])


def test_budget_gate_callback_noop_when_off() -> None:
    cb = p._BudgetGateCallback(tenant_id="t", agent="team_manager")
    cb.on_chat_model_start({}, [])  # enforcement off (default) -> no raise


def test_resolve_attaches_budget_gate_and_usage_callbacks() -> None:
    # resolve_chat_model attaches BOTH the budget gate and the Migration-173 usage callback so
    # every model's calls are metered exactly once (the usage callback owns record_llm_call).
    from orchestrator.llm.usage_callback import LlmUsageCallback

    m = p.resolve_chat_model("complex", agent="team_manager", tenant_id="t")
    kinds = {type(cb).__name__ for cb in (m.callbacks or [])}
    assert "_BudgetGateCallback" in kinds
    assert any(isinstance(cb, LlmUsageCallback) for cb in (m.callbacks or []))
