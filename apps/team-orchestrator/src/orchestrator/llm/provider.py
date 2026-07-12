"""VT-619b — provider-agnostic model resolution (Anthropic + OpenAI GPT-5.6).

The one seam every model construction routes through. Two entry points:

  * ``resolve_chat_model(tier, *, agent, tenant_id=...)`` -> a langchain ``BaseChatModel``
    (``ChatAnthropic`` or ``ChatOpenAI`` on the Responses API) for the langchain agent/lane sites.
  * ``resolve_model_id(tier)`` -> the raw model-id string for the direct Anthropic Messages-SDK
    sites (triage / classifiers / plan-validation); those stay Anthropic-only in v1 and call
    ``require_anthropic_model`` to fail LOUD if their tier is pointed at a gpt-* id.

Model choice is per-call from the ``TEAM_MODEL_*`` env tier mapping (read FRESH every call — no
import-time freeze), so swapping claude-* ↔ gpt-5.6-* is a Railway ENV change, never a code change.

GPT-5.6 facts (OpenAI docs, 2026-07-13):
  * GPT-5.6 needs the RESPONSES API for reasoning / tool-calling / multi-turn — ``ChatOpenAI`` with
    ``use_responses_api=True`` (verified against langchain-openai==1.2.2: native fields
    ``use_responses_api`` / ``service_tier`` / ``reasoning_effort`` / ``request_timeout``).
  * FLEX processing: ``service_tier="flex"`` bills at batch rates; default request timeout is 10 min,
    widen to the 15-min ceiling for flex; the SDK auto-retries the 408 (timeout) class twice
    (``max_retries``); a 429 "Resource Unavailable" is NOT charged -> backoff-retry then fall back to
    ``service_tier="auto"`` (``invoke_with_flex_fallback``).
  * GPT-5.6 REJECTS ``temperature`` like sonnet/opus — ``llm_config.sampling_kwargs`` returns ``{}``
    for gpt-*, so no temperature is sent (same guard that keeps sonnet/opus off the deprecated param).

Metering + budget are wired but decoupled from the parallel builds:
  * usage recording is the Migration-173 ``orchestrator.llm.usage_callback.LlmUsageCallback``
    (its ``(tenant_id, agent, call_site)`` ctor is fixed "for the provider seam"); it fires
    ``ledger.record_llm_call`` on every LLM end. Attached lazily + fail-soft — metering NEVER breaks
    a live turn (CL-122) and never blocks model construction.
  * a pre-call budget hook calls ``orchestrator.llm.budget_gate.check_llm_budget`` (lazy + fail-soft);
    on a 'hard' verdict it raises ``BudgetExceededError`` the caller can catch. DEFAULT OFF via
    ``TEAM_LLM_BUDGET_ENFORCE`` until Fazal flips it — wiring the caller's degrade behaviour is a
    later item; this only makes the signal available.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from orchestrator.llm_config import sampling_kwargs

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("orchestrator.llm.provider")

# ---------------------------------------------------------------------------
# Model registry — the ONLY supported ids. An id outside this set fails LOUD at
# resolve time (a typo in a TEAM_MODEL_* env var must never silently pick a
# wrong/unknown model). Provider is inferred by prefix (gpt-* / claude-*).
# ---------------------------------------------------------------------------
ANTHROPIC_MODELS: frozenset[str] = frozenset(
    {
        "claude-haiku-4-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
    }
)
OPENAI_MODELS: frozenset[str] = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
)
SUPPORTED_MODELS: frozenset[str] = ANTHROPIC_MODELS | OPENAI_MODELS

# Env tier mapping: (env var, default model id). Read fresh per call. NO env-suffix on the var
# names — one canonical NAME across dev/prod (standing rule, Fazal 2026-06-26).
_TIER_DEFAULTS: dict[str, tuple[str, str]] = {
    "routine": ("TEAM_MODEL_ROUTINE", "claude-haiku-4-5"),
    "complex": ("TEAM_MODEL_COMPLEX", "claude-sonnet-5"),
    "classifier": ("TEAM_MODEL_CLASSIFIER", "claude-haiku-4-5"),
    "specialist": ("TEAM_MODEL_SPECIALIST", "claude-sonnet-5"),
    "review": ("TEAM_MODEL_REVIEW", "claude-opus-4-8"),
}

_DEFAULT_MAX_TOKENS = 4096
# FLEX: default request timeout is 10 min; a flex job can run longer, so widen to the documented
# 15-min ceiling. Non-flex leaves the client default in place (None).
_FLEX_TIMEOUT_S = 900.0
# The SDK auto-retries the 408 (timeout) class twice on flex — mirror it.
_OPENAI_MAX_RETRIES = 2
# One backoff before the flex -> auto fallback on a 429 "Resource Unavailable".
_FLEX_BACKOFF_S = 2.0


class UnknownModelError(ValueError):
    """A model id / tier that is not one of the supported six (or a gpt-* id at an
    Anthropic-only call site). Raised LOUD at resolve time — a clear error beats a
    silent wrong-provider call."""


class BudgetExceededError(RuntimeError):
    """A pre-call budget check returned a 'hard' verdict for (tenant, agent). Raised by
    ``enforce_budget`` when ``TEAM_LLM_BUDGET_ENFORCE`` is on; callers catch it to degrade.
    Default OFF until Fazal flips the flag."""

    def __init__(self, tenant_id: Any, agent: str) -> None:
        self.tenant_id = tenant_id
        self.agent = agent
        super().__init__(f"LLM budget exceeded (hard) for tenant={tenant_id} agent={agent}")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def provider_for(model_id: str) -> str:
    """Infer the provider from the model-id prefix. Raises ``UnknownModelError`` for anything
    that is neither a gpt-* nor a claude-* id."""
    if model_id.startswith("gpt-"):
        return "openai"
    if model_id.startswith("claude-"):
        return "anthropic"
    raise UnknownModelError(
        f"Cannot infer provider for model id {model_id!r}: expected a 'gpt-*' or 'claude-*' "
        f"prefix. Supported models: {', '.join(sorted(SUPPORTED_MODELS))}."
    )


def _assert_supported(model_id: str) -> None:
    if model_id not in SUPPORTED_MODELS:
        raise UnknownModelError(
            f"Unsupported LLM model id {model_id!r}. Supported models are: "
            f"{', '.join(sorted(SUPPORTED_MODELS))}. Check the relevant TEAM_MODEL_* env var."
        )


def resolve_model_id(tier: str) -> str:
    """Resolve ``tier`` -> a concrete model id via the ``TEAM_MODEL_*`` env mapping, read FRESH.

    Fails LOUD (``ValueError``) on an unknown tier and (``UnknownModelError``) on a model id
    outside the supported six.
    """
    try:
        env_var, default = _TIER_DEFAULTS[tier]
    except KeyError:
        raise ValueError(
            f"Unknown LLM tier {tier!r}; expected one of {', '.join(sorted(_TIER_DEFAULTS))}."
        ) from None
    model_id = (os.environ.get(env_var) or "").strip() or default
    _assert_supported(model_id)
    return model_id


def require_anthropic_model(model_id: str, *, site: str) -> str:
    """Assert ``model_id`` is an Anthropic model, else fail LOUD. Guards the direct Messages-SDK
    call sites (triage / classifiers / plan-validation) which are Anthropic-only in v1 — a gpt-*
    tier value there is a config error, and a clear error beats a silent wrong-provider call."""
    if provider_for(model_id) != "anthropic":
        raise UnknownModelError(
            f"Call site {site!r} is Anthropic-SDK-only (v1) but its tier resolved to {model_id!r} "
            f"(provider=openai). Port {site} to the multi-provider seam before pointing its tier at "
            f"a GPT model, or set its TEAM_MODEL_* var back to a claude-* id."
        )
    return model_id


def _configured_service_tier() -> str:
    """The Viabe-facing OpenAI service tier from ``TEAM_OPENAI_SERVICE_TIER`` (standard|flex|auto,
    default standard). 'standard' means "no special tier" — the OpenAI request omits service_tier."""
    raw = (os.environ.get("TEAM_OPENAI_SERVICE_TIER") or "standard").strip().lower()
    if raw not in {"standard", "flex", "auto"}:
        logger.warning(
            "TEAM_OPENAI_SERVICE_TIER=%r invalid (expected standard|flex|auto); using 'standard'",
            raw,
        )
        return "standard"
    return raw


def _api_service_tier(configured: str) -> str | None:
    """Map the Viabe-facing tier to the OpenAI API ``service_tier`` value. 'standard' -> None
    (omit the field; OpenAI uses the account default — 'standard' is not an OpenAI enum value)."""
    return None if configured == "standard" else configured


def resolve_chat_model(
    tier: str,
    *,
    agent: str,
    tenant_id: UUID | str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> BaseChatModel:
    """Build the langchain chat model for ``tier`` (ChatAnthropic or ChatOpenAI-on-Responses-API).

    Attaches the per-model seam callbacks: a pre-call budget hook (``enforce_budget``; default OFF)
    and the usage-recording ledger callback (Migration-173 ``LlmUsageCallback`` → ``record_llm_call``,
    lazy + fail-soft). ``agent`` / ``tenant_id`` are the metering attribution for this model's calls
    (``call_site`` == ``tier``).
    """
    model_id = resolve_model_id(tier)
    provider = provider_for(model_id)
    configured_tier = _configured_service_tier() if provider == "openai" else "standard"
    callbacks = _seam_callbacks(tier=tier, agent=agent, tenant_id=tenant_id)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs (parity with
        # the pre-seam ctors in dispatch / the lanes). sampling_kwargs pins temp=0 only on haiku.
        return ChatAnthropic(  # type: ignore[call-arg]
            model=model_id,
            max_tokens=max_tokens,
            callbacks=callbacks,
            **sampling_kwargs(model_id),
        )

    return _build_openai_chat_model(
        model_id, max_tokens=max_tokens, configured_tier=configured_tier, callbacks=callbacks,
    )


def _build_openai_chat_model(
    model_id: str,
    *,
    max_tokens: int,
    configured_tier: str,
    callbacks: list[BaseCallbackHandler],
) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    api_tier = _api_service_tier(configured_tier)
    # Widen the request timeout to the 15-min flex ceiling only for flex; else leave the client
    # default (None). max_retries mirrors the SDK's 408-class auto-retry (twice).
    request_timeout = _FLEX_TIMEOUT_S if configured_tier == "flex" else None
    return ChatOpenAI(  # type: ignore[call-arg]
        model=model_id,
        # GPT-5.6 needs the Responses API for reasoning / tool-calling / multi-turn.
        use_responses_api=True,
        service_tier=api_tier,
        max_tokens=max_tokens,
        max_retries=_OPENAI_MAX_RETRIES,
        request_timeout=request_timeout,
        callbacks=callbacks,
        # gpt-* -> {} (no temperature), same guard as the anthropic path.
        **sampling_kwargs(model_id),
    )


# ---------------------------------------------------------------------------
# Flex fallback
# ---------------------------------------------------------------------------
def _is_openai_resource_unavailable(exc: BaseException) -> bool:
    """True for an OpenAI 429 "Resource Unavailable" (flex batch capacity). That 429 is NOT charged
    (docs 2026-07-13), so it is safe to backoff-retry / fall back to service_tier='auto'."""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status == 429


def _rebuild_openai_at_auto(model: Any, callbacks: list[BaseCallbackHandler]) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    model_id = getattr(model, "model_name", None) or getattr(model, "model", "")
    return ChatOpenAI(  # type: ignore[call-arg]
        model=model_id,
        use_responses_api=True,
        service_tier="auto",
        max_tokens=getattr(model, "max_tokens", _DEFAULT_MAX_TOKENS),
        max_retries=_OPENAI_MAX_RETRIES,
        callbacks=callbacks,
        **sampling_kwargs(model_id),
    )


def invoke_with_flex_fallback(model: Any, model_input: Any, **kwargs: Any) -> Any:
    """Invoke ``model``, falling back off flex on a 429 "Resource Unavailable": one backoff-retry
    on flex, then rebuild the model at ``service_tier="auto"`` and invoke once more. Any other
    error (and any Anthropic / non-flex model) passes straight through unchanged.

    The seam ships this helper so a flex-configured caller can opt in; v1 does not force every call
    site through it (flex defaults OFF via TEAM_OPENAI_SERVICE_TIER=standard)."""
    try:
        return model.invoke(model_input, **kwargs)
    except Exception as exc:  # noqa: BLE001 — re-raised unless it's the retryable 429 class
        if not _is_openai_resource_unavailable(exc):
            raise
        logger.warning("OpenAI flex 429 (resource unavailable); backoff %.1fs then retry", _FLEX_BACKOFF_S)
        time.sleep(_FLEX_BACKOFF_S)
        try:
            return model.invoke(model_input, **kwargs)
        except Exception as exc2:  # noqa: BLE001
            if not _is_openai_resource_unavailable(exc2):
                raise
            logger.warning("OpenAI flex still 429; falling back to service_tier='auto'")
            fallback = _rebuild_openai_at_auto(model, getattr(model, "callbacks", None) or [])
            return fallback.invoke(model_input, **kwargs)


# ---------------------------------------------------------------------------
# Budget gate (pre-call) — lazy + fail-soft; default OFF
# ---------------------------------------------------------------------------
def _budget_enforce_on() -> bool:
    return (os.environ.get("TEAM_LLM_BUDGET_ENFORCE") or "").strip().lower() in {"1", "true", "yes"}


def _verdict_is_hard(result: Any) -> bool:
    """Robust to the parallel budget_gate return shape: a level string ('hard'), an object with a
    ``.level`` attr, or a dict with a 'level' key."""
    if result is None:
        return False
    level = getattr(result, "level", None)
    if level is None and isinstance(result, dict):
        level = result.get("level")
    if level is None and isinstance(result, str):
        level = result
    return str(level).strip().lower() == "hard"


def enforce_budget(tenant_id: Any, agent: str) -> None:
    """Pre-call budget hook. No-op unless ``TEAM_LLM_BUDGET_ENFORCE`` is on. When on, calls
    ``budget_gate.check_llm_budget`` (lazy import + fail-soft) and raises ``BudgetExceededError`` on
    a 'hard' verdict. Any failure INSIDE the gate itself fails soft (never blocks a turn); only the
    typed BudgetExceededError propagates."""
    if not _budget_enforce_on():
        return
    try:
        from orchestrator.llm.budget_gate import check_llm_budget

        verdict = check_llm_budget(tenant_id, agent)
    except Exception:  # noqa: BLE001 — gate must never break a turn on its own error (CL-122)
        logger.warning("LLM budget gate errored; failing OPEN", exc_info=True)
        return
    if _verdict_is_hard(verdict):
        raise BudgetExceededError(tenant_id, agent)


# ---------------------------------------------------------------------------
# Per-model seam callbacks: budget gate (mine) + usage recording (parallel Migration-173 callback)
# ---------------------------------------------------------------------------
class _BudgetGateCallback(BaseCallbackHandler):
    """Model-bound pre-call budget gate attached in ``resolve_chat_model``. On start it calls
    ``enforce_budget`` — the ONLY thing allowed to ABORT an LLM call (the typed BudgetExceededError
    propagates via ``raise_error=True``; any other failure fails soft). Usage RECORDING is a
    SEPARATE callback (``orchestrator.llm.usage_callback.LlmUsageCallback``) so the two concerns
    (a hard gate that may raise vs. a best-effort observer that must not) stay cleanly split."""

    # Let the explicit BudgetExceededError raised in on_*_start propagate out of langchain's
    # callback manager (rather than being logged+swallowed by langchain).
    raise_error = True

    def __init__(self, *, tenant_id: UUID | str | None, agent: str) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.agent = agent

    def _on_start(self) -> None:
        try:
            enforce_budget(self.tenant_id, self.agent)
        except BudgetExceededError:
            raise
        except Exception:  # noqa: BLE001 — defensive; enforce_budget already fails soft internally
            logger.warning("LLM budget gate hook errored; continuing", exc_info=True)

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        self._on_start()

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[Any], **kwargs: Any
    ) -> None:
        # ChatAnthropic / ChatOpenAI fire on_chat_model_start (chat models), not on_llm_start.
        self._on_start()


def _seam_callbacks(
    *, tier: str, agent: str, tenant_id: UUID | str | None
) -> list[BaseCallbackHandler]:
    """The callbacks ``resolve_chat_model`` attaches to every model: the pre-call budget gate plus
    the usage-recording ``LlmUsageCallback`` (Migration-173, the parallel cost-ledger seam) with the
    provider-fixed ``(tenant_id, agent, call_site)`` ctor. The usage callback is imported LAZILY +
    fail-soft — a metering-module hiccup must never break model construction or a live turn."""
    callbacks: list[BaseCallbackHandler] = [_BudgetGateCallback(tenant_id=tenant_id, agent=agent)]
    try:
        from orchestrator.llm.usage_callback import LlmUsageCallback

        callbacks.append(LlmUsageCallback(tenant_id, agent, tier))
    except Exception:  # noqa: BLE001 — CL-122: usage metering is best-effort, never load-bearing
        logger.warning("LlmUsageCallback unavailable; usage metering skipped this build", exc_info=True)
    return callbacks
