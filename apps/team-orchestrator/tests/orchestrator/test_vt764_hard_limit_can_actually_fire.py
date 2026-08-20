"""VT-764 — the runaway guard now fires on a runaway and NOT on every run.

Two defects, and they had to be fixed together:

1. The token axis counted `tokens_input + tokens_output` against 10,000 while the brain's prompt
   alone measures ~17k, so the check was TRUE on the first call of EVERY run.
2. The raise happened inside a langchain callback whose exceptions the callback manager catches and
   logs, so a breach detected at an LLM or tool boundary aborted nothing.

   Scoped precisely (my first version of this overclaimed): `dispatch_brain`'s
   `except HardLimitExceeded` branch is NOT dead — 74 dev runs carry `status='aborted_hard_limit'`
   from some non-callback raiser. What was dead is the callback-originated abort.

Fixing (2) alone would have aborted every brain run. So the tests below pin BOTH halves: the real
measured prompt size passes, a runaway output aborts, and a limit breach genuinely escapes the
callback while an observability failure still cannot.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("dbos")

from orchestrator.agent.orchestrator_agent_driver import (  # noqa: E402
    ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT,
    ORCHESTRATOR_TOOL_CALL_HARD_LIMIT,
    HardLimitExceeded,
    OrchestratorAgentDriver,
    OrchestratorUsage,
)
from orchestrator.observability import langchain_callback as cb_mod  # noqa: E402
from orchestrator.observability.langchain_callback import (  # noqa: E402
    OrchestratorReasoningCallback,
)

# MEASURED on deployed dev (llm_call_events, 7 days to 2026-08-17), call_site='complex':
# input p50 16,780 / max 17,656; output per-invocation p50 42 / p95 359 / max 728.
_REAL_PROMPT_TOKENS = 17_656
_REAL_OUTPUT_MAX = 728


def _driver() -> OrchestratorAgentDriver:
    return OrchestratorAgentDriver(agent=object(), model_name="test-model")


def _ids():
    return {"run_id": uuid4(), "tenant_id": uuid4()}


# --- the axis ------------------------------------------------------------------------------


def test_the_brains_real_measured_spend_does_not_trip_the_guard():
    """The regression this row exists for: at the retired combined 10k limit this raised on call one
    of every run, because 17,656 input tokens is the PROMPT, not a runaway."""
    usage = OrchestratorUsage(
        tokens_input=_REAL_PROMPT_TOKENS, tokens_output=_REAL_OUTPUT_MAX, tool_calls=6
    )

    _driver().check_mid_invocation(usage, **_ids())  # must not raise


def test_a_huge_prompt_alone_is_never_a_breach():
    """Even four times the measured prompt is not a runaway signal — prompt growth has its own gate."""
    usage = OrchestratorUsage(tokens_input=_REAL_PROMPT_TOKENS * 4, tokens_output=0)

    _driver().check_mid_invocation(usage, **_ids())


def test_runaway_output_aborts_on_the_output_axis():
    usage = OrchestratorUsage(
        tokens_input=_REAL_PROMPT_TOKENS,
        tokens_output=ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT + 1,
    )

    with pytest.raises(HardLimitExceeded) as exc:
        _driver().check_mid_invocation(usage, **_ids())

    assert exc.value.axis == "tokens_output"
    assert exc.value.observed == ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT + 1


def test_the_output_limit_clears_the_largest_output_ever_observed():
    """4,946 is the biggest per-invocation output measured anywhere in the system (sr_draft_turn).
    A limit under that would abort legitimate work; the check exists so a future tightening has to
    confront the measurement."""
    assert ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT > 4_946


def test_the_null_driver_and_the_real_driver_cannot_diverge_on_the_axis():
    """VT-617's lesson: a hardcoded 5 in this stand-in silently SHADOWED the raised tool-call limit
    and truncated multi-tool turns. Same failure would hide here."""
    from orchestrator.agent.dispatch import _NullDriver

    assert _NullDriver.output_token_limit == ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT
    assert _NullDriver.tool_call_limit == ORCHESTRATOR_TOOL_CALL_HARD_LIMIT

    breach = OrchestratorUsage(tokens_output=ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT + 1)
    with pytest.raises(HardLimitExceeded) as exc:
        _NullDriver().check_mid_invocation(breach, **_ids())
    assert exc.value.axis == "tokens_output"

    ok = OrchestratorUsage(tokens_input=_REAL_PROMPT_TOKENS, tokens_output=_REAL_OUTPUT_MAX)
    _NullDriver().check_mid_invocation(ok, **_ids())


# --- the guard can now abort ---------------------------------------------------------------


class _AlwaysBreaches:
    tool_call_limit = 10
    output_token_limit = 8_000
    wall_clock_limit_s = 120.0
    cost_limit_paise = 500

    def check_mid_invocation(self, usage, *, run_id, tenant_id) -> None:
        raise HardLimitExceeded(
            axis="tokens_output", observed=99_999, limit=8_000, run_id=run_id, tenant_id=tenant_id
        )


class _NeverBreaches:
    tool_call_limit = 10
    output_token_limit = 8_000
    wall_clock_limit_s = 120.0
    cost_limit_paise = 500

    def check_mid_invocation(self, usage, *, run_id, tenant_id) -> None:
        return None


def _callback(driver, usage: OrchestratorUsage | None = None):
    return OrchestratorReasoningCallback(
        driver=driver, usage=usage or OrchestratorUsage(), run_id=uuid4(), tenant_id=uuid4()
    )


def test_the_callback_declares_raise_error():
    """Without this, langchain's callback manager catches every HardLimitExceeded raised at an LLM or
    tool boundary and logs `Error in ... callback:` — which is exactly what happened on every run for
    months, while the non-callback raisers could still abort."""
    assert OrchestratorReasoningCallback.raise_error is True


def test_a_breach_escapes_on_the_llm_start_boundary():
    with pytest.raises(HardLimitExceeded):
        _callback(_AlwaysBreaches()).on_chat_model_start({}, [])


def test_a_breach_escapes_on_the_llm_end_boundary():
    with pytest.raises(HardLimitExceeded):
        _callback(_AlwaysBreaches()).on_llm_end(object())


def test_a_breach_escapes_before_the_tool_runs():
    with pytest.raises(HardLimitExceeded):
        _callback(_AlwaysBreaches()).on_tool_start({"name": "x"}, "input")


# --- and nothing else escapes --------------------------------------------------------------


def test_an_accounting_failure_does_not_cost_the_turn(monkeypatch):
    """raise_error makes ANY handler exception propagate, so the observability work is guarded. A
    write_step or usage-parse failure must still never break a turn (CL-122)."""
    call = _callback(_NeverBreaches())
    monkeypatch.setattr(
        call, "_account_and_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    call.on_llm_end(object())  # must not raise


def test_the_limit_check_still_runs_after_an_accounting_failure(monkeypatch):
    """The guard must not be reachable only on the happy path — a breach has to abort even when the
    accounting above it blew up."""
    call = _callback(_AlwaysBreaches())
    monkeypatch.setattr(
        call, "_account_and_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(HardLimitExceeded):
        call.on_llm_end(object())


def test_a_failing_audit_write_does_not_escape_on_tool_start(monkeypatch):
    monkeypatch.setattr(
        cb_mod, "emit_tm_audit", lambda **kw: (_ for _ in ()).throw(RuntimeError("audit down"))
    )
    usage = OrchestratorUsage()
    call = _callback(_NeverBreaches(), usage)

    call.on_tool_start({"name": "read_onboarding_state"}, "{}")

    assert usage.tool_calls == 1, "the counter must still advance when the audit write fails"


def test_a_failing_audit_write_does_not_escape_on_tool_end(monkeypatch):
    monkeypatch.setattr(
        cb_mod, "emit_tm_audit", lambda **kw: (_ for _ in ()).throw(RuntimeError("audit down"))
    )

    _callback(_NeverBreaches()).on_tool_end("result")  # must not raise


def test_a_failing_node_stash_does_not_escape(monkeypatch):
    call = _callback(_NeverBreaches())
    monkeypatch.setattr(
        call, "_stash_node", lambda **kw: (_ for _ in ()).throw(RuntimeError("stash down"))
    )

    call.on_chat_model_start({}, [])  # must not raise


# --- the other axes are untouched ----------------------------------------------------------


def test_the_tool_call_axis_still_fires():
    usage = OrchestratorUsage(tool_calls=ORCHESTRATOR_TOOL_CALL_HARD_LIMIT + 1)

    with pytest.raises(HardLimitExceeded) as exc:
        _driver().check_mid_invocation(usage, **_ids())

    assert exc.value.axis == "tool_calls"


def test_the_cost_axis_still_fires():
    usage = OrchestratorUsage(cost_paise=100_000)

    with pytest.raises(HardLimitExceeded) as exc:
        _driver().check_mid_invocation(usage, **_ids())

    assert exc.value.axis == "cost_paise"
