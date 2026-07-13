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
pytest.importorskip("langchain_google_genai")

from orchestrator.llm import provider as p  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee unset TEAM_MODEL_* / service-tier / budget / GLM_BASE_URL / XAI_BASE_URL /
    TEAM_ENABLE_WEB_SEARCH env so tests see the built-in defaults, and dummy OPENAI_API_KEY /
    GOOGLE_API_KEY / GLM_API_KEY / XAI_API_KEY so the ChatOpenAI / ChatGoogleGenerativeAI / GLM /
    Grok ctors never fail on a missing credential."""
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
        "TEAM_ENABLE_WEB_SEARCH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GOOGLE_API_KEY", "gk-test-not-real")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-not-real")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-real")


# --------------------------------------------------------------------------- registry
def test_registry_has_exactly_twelve_supported() -> None:
    assert (
        p.SUPPORTED_MODELS
        == p.ANTHROPIC_MODELS | p.OPENAI_MODELS | p.GOOGLE_MODELS | p.ZAI_MODELS | p.XAI_MODELS
    )
    assert len(p.SUPPORTED_MODELS) == 12
    assert p.ANTHROPIC_MODELS == {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}
    assert p.OPENAI_MODELS == {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    assert p.GOOGLE_MODELS == {
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.1-pro-preview",
    }
    assert p.ZAI_MODELS == {"glm-5.2"}
    assert p.XAI_MODELS == {"grok-4.5", "grok-4.3"}


# --------------------------------------------------------------------------- provider inference
def test_provider_inference_by_prefix() -> None:
    assert p.provider_for("claude-sonnet-5") == "anthropic"
    assert p.provider_for("gpt-5.6-terra") == "openai"
    assert p.provider_for("gemini-3.5-flash") == "google"
    assert p.provider_for("glm-5.2") == "zai"
    assert p.provider_for("grok-4.5") == "xai"


def test_provider_inference_unknown_prefix_raises() -> None:
    # A prefix that is none of gpt-* / claude-* / gemini-* / glm-* / grok-*.
    with pytest.raises(p.UnknownModelError):
        p.provider_for("mistral-large-2")


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
    monkeypatch.setenv("TEAM_MODEL_COMPLEX", "gpt-4o")  # a real model, but NOT one of the ten
    with pytest.raises(p.UnknownModelError) as exc:
        p.resolve_model_id("complex")
    # the error names the full supported set (now twelve models, incl. gemini + glm + grok families)
    msg = str(exc.value)
    assert "gpt-5.6-terra" in msg
    assert "gemini-3.5-flash" in msg
    assert "glm-5.2" in msg
    assert "grok-4.5" in msg


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


def test_flex_billing_tier_reaches_usage_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flex OpenAI call must record service_tier='flex' on the ledger (else cost is 2x
    overstated). The billing tier threads provider -> _seam_callbacks -> LlmUsageCallback."""
    from orchestrator.llm.usage_callback import LlmUsageCallback

    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gpt-5.6-sol")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    uc = next(cb for cb in (m.callbacks or []) if isinstance(cb, LlmUsageCallback))
    assert uc.service_tier == "flex"


def test_standard_billing_tier_on_anthropic_and_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """anthropic calls + OpenAI 'auto' (server-picks the tier) record 'standard' — never
    under-cost a call whose billed rate we can't know at write time."""
    from orchestrator.llm.usage_callback import LlmUsageCallback

    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gpt-5.6-sol")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "auto")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    uc = next(cb for cb in (m.callbacks or []) if isinstance(cb, LlmUsageCallback))
    assert uc.service_tier == "standard"


# --------------------------------------------------------------------------- google (Gemini)
def test_resolve_chat_model_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gemini-3.5-flash")
    m = p.resolve_chat_model("specialist", agent="finance_lane", max_tokens=2048)
    assert type(m).__name__ == "ChatGoogleGenerativeAI"
    assert m.model == "gemini-3.5-flash"
    # max_tokens maps to the ctor's max_output_tokens; Gemini ACCEPTS temperature -> pinned 0.0.
    assert m.max_output_tokens == 2048
    assert m.temperature == 0.0


def test_google_ignores_openai_service_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    # TEAM_OPENAI_SERVICE_TIER is OpenAI-scoped by name — a flex setting must NOT affect a google
    # call (flex/batch are not wired for google in v1). The model still builds as a Gemini model.
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gemini-3.1-pro-preview")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    assert type(m).__name__ == "ChatGoogleGenerativeAI"
    assert m.model == "gemini-3.1-pro-preview"


def test_resolve_google_attaches_seam_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same metering contract as the anthropic/openai paths: both the budget gate and the
    # Migration-173 usage callback attach so google calls are metered exactly once.
    from orchestrator.llm.usage_callback import LlmUsageCallback

    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gemini-3.5-flash")
    m = p.resolve_chat_model("specialist", agent="finance_lane", tenant_id="t")
    kinds = {type(cb).__name__ for cb in (m.callbacks or [])}
    assert "_BudgetGateCallback" in kinds
    assert any(isinstance(cb, LlmUsageCallback) for cb in (m.callbacks or []))


def test_require_anthropic_model_rejects_gemini() -> None:
    with pytest.raises(p.UnknownModelError) as exc:
        p.require_anthropic_model("gemini-3.5-flash", site="triage")
    assert "triage" in str(exc.value)
    assert "google" in str(exc.value)


# --------------------------------------------------------------------------- zai (GLM)
def test_resolve_chat_model_glm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # GLM reuses ChatOpenAI but with plain chat completions (NOT the Responses API), the z.ai
    # default base_url, no service_tier, and temperature pinned 0.0 (Gemini/haiku-style).
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "glm-5.2")
    m = p.resolve_chat_model("specialist", agent="finance_lane", max_tokens=2048)
    assert type(m).__name__ == "ChatOpenAI"
    assert m.model_name == "glm-5.2"
    assert m.use_responses_api is False
    assert str(m.openai_api_base) == p._GLM_DEFAULT_BASE_URL
    assert m.service_tier is None
    assert m.temperature == 0.0


def test_glm_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # GLM_BASE_URL is the single self-host switch — a custom endpoint lands on the client.
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "glm-5.2")
    monkeypatch.setenv("GLM_BASE_URL", "http://localhost:8000/v1/")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    assert str(m.openai_api_base) == "http://localhost:8000/v1/"


def test_glm_ignores_openai_service_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    # TEAM_OPENAI_SERVICE_TIER is OpenAI-scoped — a flex setting must NOT wire flex onto a GLM call.
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "glm-5.2")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("specialist", agent="finance_lane")
    assert m.service_tier is None
    assert m.request_timeout is None


def test_resolve_glm_attaches_seam_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.llm.usage_callback import LlmUsageCallback

    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "glm-5.2")
    m = p.resolve_chat_model("specialist", agent="finance_lane", tenant_id="t")
    kinds = {type(cb).__name__ for cb in (m.callbacks or [])}
    assert "_BudgetGateCallback" in kinds
    assert any(isinstance(cb, LlmUsageCallback) for cb in (m.callbacks or []))


# --------------------------------------------------------------------------- xai (Grok)
def test_resolve_chat_model_grok_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Grok reuses ChatOpenAI on the RESPONSES API (like gpt-5.6) but pointed at the x.ai base_url,
    # with NO service_tier and temperature pinned 0.0 (Grok ACCEPTS temperature, unlike gpt-5.6).
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "grok-4.5")
    m = p.resolve_chat_model("specialist", agent="advisor_lane", max_tokens=2048)
    assert type(m).__name__ == "ChatOpenAI"
    assert m.model_name == "grok-4.5"
    assert m.use_responses_api is True
    assert str(m.openai_api_base) == p._XAI_DEFAULT_BASE_URL
    assert m.service_tier is None
    assert m.request_timeout is None
    assert m.temperature == 0.0
    assert m.max_retries == p._OPENAI_MAX_RETRIES


def test_grok_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # XAI_BASE_URL is the single proxy/self-host switch — a custom endpoint lands on the client.
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "grok-4.3")
    monkeypatch.setenv("XAI_BASE_URL", "http://localhost:9000/v1")
    m = p.resolve_chat_model("specialist", agent="advisor_lane")
    assert str(m.openai_api_base) == "http://localhost:9000/v1"


def test_grok_ignores_openai_service_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    # TEAM_OPENAI_SERVICE_TIER is OpenAI-scoped by NAME — a flex setting must NOT reach a Grok call
    # (xAI publishes no flex/batch tier; grok always records standard).
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "grok-4.5")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("specialist", agent="advisor_lane")
    assert m.service_tier is None
    assert m.request_timeout is None


def test_grok_billing_tier_is_standard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with TEAM_OPENAI_SERVICE_TIER=flex set, the Grok call records 'standard' on the ledger
    # (the flex discount is OpenAI-only; xai must never be under-costed).
    from orchestrator.llm.usage_callback import LlmUsageCallback

    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "grok-4.5")
    monkeypatch.setenv("TEAM_OPENAI_SERVICE_TIER", "flex")
    m = p.resolve_chat_model("specialist", agent="advisor_lane")
    uc = next(cb for cb in (m.callbacks or []) if isinstance(cb, LlmUsageCallback))
    assert uc.service_tier == "standard"


def test_resolve_grok_attaches_seam_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.llm.usage_callback import LlmUsageCallback

    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "grok-4.5")
    m = p.resolve_chat_model("specialist", agent="advisor_lane", tenant_id="t")
    kinds = {type(cb).__name__ for cb in (m.callbacks or [])}
    assert "_BudgetGateCallback" in kinds
    assert any(isinstance(cb, LlmUsageCallback) for cb in (m.callbacks or []))


def test_require_anthropic_model_rejects_grok() -> None:
    with pytest.raises(p.UnknownModelError) as exc:
        p.require_anthropic_model("grok-4.5", site="triage")
    assert "triage" in str(exc.value)
    assert "xai" in str(exc.value)


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
