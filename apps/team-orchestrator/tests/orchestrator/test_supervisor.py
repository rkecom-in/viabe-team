"""Supervisor-graph tests (VT-3.4 PR 1/3 + PR 2/3).

PR 1/3 (CL-129): ONE happy-path integration test —
``test_orchestrator_spawns_sales_recovery_returns_campaign_plan`` — two real
Anthropic calls, ``@pytest.mark.integration``, additionally guarded on
ANTHROPIC_API_KEY.

PR 2/3 (CL-202 / CL-203): the landmine-1 precedence test —
``test_supervisor_graph_spawn_vs_no_spawn_precedence``. Keyless: it runs the
REAL ``build_supervisor_graph`` / ``create_agent`` / ``Command.PARENT`` /
conditional edge, substituting only the model layer with a fake. It exercises
the undocumented precedence between the spawn tool's
``Command(goto=..., graph=Command.PARENT)`` and the ``add_conditional_edges``
after the orchestrator node, and captures the observed behaviour.

Module-level imports run after the importorskip guards, so collecting this
file in the CI ``orchestrator`` job import-checks the whole supervisor chain.
"""

from __future__ import annotations

import os
import warnings
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from langchain_anthropic import ChatAnthropic  # noqa: E402 — after importorskip
from langchain_core.language_models import LanguageModelInput  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.runnables import Runnable  # noqa: E402

from orchestrator import routing  # noqa: E402
from orchestrator.agent.schemas.campaign_plan import (  # noqa: E402
    CampaignPlanInsufficientData,
    CampaignPlanOutOfScope,
    CampaignPlanProposed,
)
from orchestrator.supervisor import build_supervisor_graph  # noqa: E402

# v1.0 discriminated union: ``isinstance`` checks use the concrete
# variant tuple, since ``CampaignPlan`` is a TypeAlias of
# ``Annotated[union, Field(discriminator=...)]`` — not a class.
_CampaignPlanVariants = (
    CampaignPlanProposed,
    CampaignPlanOutOfScope,
    CampaignPlanInsufficientData,
)


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or not os.environ.get("DATABASE_URL"),
    reason=(
        "real-supervisor integration test needs ANTHROPIC_API_KEY "
        "(for the orchestrator + agent + self_evaluate calls) + "
        "DATABASE_URL (the collapse node persists to campaigns / "
        "subscriber_states). RUN_INTEGRATION_TESTS=1 alone is "
        "insufficient — all three env gates are required and independent."
    ),
)
def test_orchestrator_spawns_sales_recovery_returns_campaign_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production live-run through the supervisor dispatch.

    VT-SR-Agent dispatch switch + CL-287 user_request threading +
    CL-288 emit-shape coercion. End-to-end production path:
    orchestrator → spawn → real agent → coercion → gate → collapse.

    Load-bearing assertions PROVE a real Opus round-trip happened on
    the specialist side — a green pass here cannot be reached by a
    mock-leak or silent SDK-substitution path. Specifically:
      (1) ``_CountingClient.calls_to_real_anthropic`` records every
          call that reaches ``self._real.messages.create(...)`` AFTER
          it returns. Specialist call count >= 1.
      (2) First specialist call's ``model='claude-opus-4-7'``.
      (3) First specialist response carries an ``id`` starting with
          ``'msg_'`` — only real Anthropic API responses do.
      (4) ``active_agent == 'sales_recovery_agent'`` — indirect proof
          the orchestrator's ChatAnthropic also fired, since the graph
          only routes there after the orchestrator invokes the
          ``spawn_sales_recovery`` tool, which requires the orchestrator
          model to have responded with tool_use.
      (5) Wall-clock floor > 2.0s — Opus + Opus single-turn each is
          typically >=1.5s; the canary makes at least two real calls
          (orchestrator + specialist) so total >2s. Weak backup per the
          CL-288 brief, not the primary gate.

    Variant assertion: ANY v1.0 CampaignPlan variant is a PASS. Empty
    ``uuid4()`` tenant has no dormant customers, so ``insufficient_data``
    is the correct verdict; asserting ``proposed`` here is the CL-288
    hallucination-pressure mistake. The seeded-fixture ``proposed``-
    path canary is CL-289's job (separate subtask, needs seed data).
    This canary verifies dispatch correctness + shape conformance,
    not plan quality.

    Env requirement is THREE-WAY, all independent and required:
      - RUN_INTEGRATION_TESTS=1 (conftest hook strips the
        @pytest.mark.integration skip; without it the marker collects
        a skip regardless of keys)
      - ANTHROPIC_API_KEY (this test's skipif)
      - DATABASE_URL (this test's skipif)
    """
    from anthropic import Anthropic as _RealAnthropic

    # Sanity: confirm the genuine SDK class is in scope.
    assert _RealAnthropic.__module__.startswith("anthropic"), (
        f"anthropic.Anthropic non-genuine: module={_RealAnthropic.__module__!r}"
    )

    class _CountingClient:
        """Real Anthropic SDK + ledger.

        Forwards every ``.messages.create(**kwargs)`` to the real client,
        records call metadata + response signature AFTER the call
        returns. CL-287 already threads the user request through
        SalesRecoveryContext, so no SDK-boundary substitution is needed
        on this branch — just count.
        """

        calls_to_real_anthropic: list[dict[str, Any]] = []

        def __init__(self) -> None:
            self._real = _RealAnthropic()

        @property
        def messages(self):  # type: ignore[no-untyped-def]
            return self

        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            response = self._real.messages.create(**kwargs)
            _CountingClient.calls_to_real_anthropic.append(
                {
                    "model": kwargs.get("model"),
                    "first_user_message": (
                        kwargs["messages"][0].get("content")
                        if kwargs.get("messages")
                        and isinstance(kwargs["messages"][0], dict)
                        else None
                    ),
                    "response_id": getattr(response, "id", None),
                    "response_usage_input": getattr(
                        getattr(response, "usage", None), "input_tokens", None
                    ),
                    "response_usage_output": getattr(
                        getattr(response, "usage", None), "output_tokens", None
                    ),
                }
            )
            return response

    # Reset the class-level ledger so prior state cannot satisfy proof.
    _CountingClient.calls_to_real_anthropic = []

    # Patch ONLY the specialist's SDK seam. The orchestrator uses
    # ChatAnthropic (langchain) which has its own SDK plumbing — that
    # path is proven indirectly via active_agent below.
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", _CountingClient
    )
    monkeypatch.setenv("VIABE_ENV", "production")  # _resolve_model → Opus

    model = ChatAnthropic(model="claude-opus-4-7")  # type: ignore[call-arg]
    graph = build_supervisor_graph(model=model)

    tenant_id = uuid4()
    run_id = uuid4()
    USER_INPUT = "Recover dormant customers from the last 60 days"

    import time

    wallclock_start = time.monotonic()
    result = graph.invoke(
        {
            "messages": [
                {"role": "user", "content": USER_INPUT}
            ],
            # spawn_sales_recovery requires run identity in state (CL-209).
            "tenant_id": tenant_id,
            "run_id": run_id,
        }
    )
    wallclock_s = time.monotonic() - wallclock_start

    # --- Exec-6.85 bundle wire-through ---------------------------------
    # The Composer bundle attached at handoff lives on the final state.
    # Inspect it BEFORE asserting so the diag captures the truth probe
    # even if a later assertion fails.
    final_bundle = result.get("sales_recovery_context")

    diag = {
        "wallclock_s": wallclock_s,
        "active_agent": result.get("active_agent"),
        "campaign_plan_type": type(result.get("campaign_plan")).__name__,
        "specialist_call_count": len(_CountingClient.calls_to_real_anthropic),
        "specialist_call_ledger": _CountingClient.calls_to_real_anthropic,
        "result_keys": sorted(result.keys()) if result else None,
        "bundle_present": final_bundle is not None,
        "bundle_user_request": (
            final_bundle.user_request if final_bundle is not None else None
        ),
        "bundle_data_completeness": (
            dict(final_bundle.data_completeness)
            if final_bundle is not None
            else None
        ),
        "bundle_trigger_reason": (
            final_bundle.trigger_reason if final_bundle is not None else None
        ),
    }

    # --- PROOF-OF-CALL: load-bearing -----------------------------------
    # (1) Specialist made >= 1 real Opus call.
    assert len(_CountingClient.calls_to_real_anthropic) >= 1, diag
    first_call = _CountingClient.calls_to_real_anthropic[0]
    # (2) Opus was the specialist target.
    assert first_call["model"] == "claude-opus-4-7", diag
    # (3) 'msg_' id prefix proves real Anthropic API response.
    assert isinstance(first_call["response_id"], str), diag
    assert first_call["response_id"].startswith("msg_"), diag
    # (4) Orchestrator dispatched (indirect proof of ChatAnthropic call —
    # graph only sets active_agent after orchestrator's tool_use turn).
    assert result.get("active_agent") == "sales_recovery_agent", diag
    # (5) Wall-clock floor: orchestrator + specialist + gate, all real
    # Opus. >=2s is a weak but reasonable lower bound.
    assert wallclock_s > 2.0, diag

    # --- Exec-6.85 wire-through assertions -----------------------------
    # (6) Composer bundle reached the final state. A None here means the
    # handoff broke and the specialist ran against no task context.
    assert final_bundle is not None, diag
    # (7) Bundle carries the orchestrator's user_request — the first user
    # message that hits the specialist's SDK call must come from the
    # bundle, not a stale hardcoded cue.
    assert final_bundle.user_request == USER_INPUT, diag
    assert first_call["first_user_message"] == USER_INPUT, diag
    # (8) Bundle identity matches the invocation state. Cross-tenant
    # contamination would show up here.
    assert final_bundle.tenant_id == tenant_id, diag
    assert final_bundle.run_id == run_id, diag
    # (9) data_completeness is structurally well-formed — five section
    # keys, all booleans. CL-190 substrate-absence makes the truth probe
    # for content emptiness (every flag False) a FINDING captured in
    # diag, not an assertion: when L1 KG / L2 episodic / campaigns /
    # owner_inputs land the values flip True without code change here.
    assert set(final_bundle.data_completeness.keys()) == {
        "business_profile",
        "customer_ledger_summary",
        "recent_campaigns",
        "attribution_snapshot",
        "pending_owner_inputs",
    }, diag

    # --- Shape conformance ---------------------------------------------
    plan = result.get("campaign_plan")
    # ANY v1.0 variant is a PASS — empty uuid4() tenant legitimately
    # yields insufficient_data. CL-288 hallucination-pressure: do NOT
    # narrow to CampaignPlanProposed here. Seeded-fixture proposed-path
    # verification is CL-289 (separate subtask, needs seed data).
    assert isinstance(plan, _CampaignPlanVariants), diag
    # Identity-injection invariant — agent overwrote model output with
    # context fields.
    assert plan.tenant_id == tenant_id, diag
    assert plan.run_id == run_id, diag


# --- Exec-6.85: cheap-model wire-through canary (Haiku) -----------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or not os.environ.get("DATABASE_URL"),
    reason=(
        "Exec-6.85 bundle canary needs ANTHROPIC_API_KEY (real model call) "
        "+ DATABASE_URL (collapse node persistence). Haiku-flavored — "
        "cheaper than the Opus canary above, used to gate the wire-through "
        "contract without burning Opus quota on every PR. RUN_INTEGRATION_TESTS=1 "
        "alone is insufficient."
    ),
)
def test_orchestrator_spawns_sales_recovery_bundle_wire_through_canary_haiku(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exec-6.85 truth probe: prove the Composer bundle reaches the
    specialist on the live path, using Haiku to keep canary cost trivial.

    Wire-through assertions (load-bearing):
      - Composer bundle present on the final state with the
        orchestrator-supplied ``user_request`` threaded in.
      - The specialist's first SDK call carries that ``user_request`` as
        the initial user message (proves the agent loop's seed message
        comes from the bundle, not a stale hardcoded cue).
      - Tenant + run identity on the bundle matches invocation state.
      - ``data_completeness`` is structurally well-formed (five section
        keys, booleans). Content emptiness with CL-190 substrate
        absence is captured in diag, not asserted (TRUTH-PROBE finding
        — the substrates land in later PRs and flip values to True
        without touching the wire-through contract).

    Proof-of-call discipline (mandatory): real billed call captured via
    ``_CountingClient``, message id has the ``msg_`` prefix, wall-clock
    floor consistent with a real round-trip. A green report without
    these signals would be a dead canary.

    Cheap-model flavor: VIABE_ENV=test resolves the specialist to
    Haiku; the orchestrator is also driven through ``ChatAnthropic``
    with Haiku to keep the second leg cheap.
    """
    import time

    from anthropic import Anthropic as _RealAnthropic

    assert _RealAnthropic.__module__.startswith("anthropic"), (
        f"anthropic.Anthropic non-genuine: module={_RealAnthropic.__module__!r}"
    )

    class _CountingClient:
        """Real Anthropic SDK + ledger — counts AFTER each call returns."""

        calls: list[dict[str, Any]] = []

        def __init__(self) -> None:
            self._real = _RealAnthropic()

        @property
        def messages(self):  # type: ignore[no-untyped-def]
            return self

        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            response = self._real.messages.create(**kwargs)
            _CountingClient.calls.append(
                {
                    "model": kwargs.get("model"),
                    "first_user_message": (
                        kwargs["messages"][0].get("content")
                        if kwargs.get("messages")
                        and isinstance(kwargs["messages"][0], dict)
                        else None
                    ),
                    "response_id": getattr(response, "id", None),
                    "response_usage_input": getattr(
                        getattr(response, "usage", None), "input_tokens", None
                    ),
                    "response_usage_output": getattr(
                        getattr(response, "usage", None), "output_tokens", None
                    ),
                }
            )
            return response

    _CountingClient.calls = []
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", _CountingClient
    )
    monkeypatch.setenv("VIABE_ENV", "test")  # _resolve_model → Haiku

    model = ChatAnthropic(model="claude-haiku-4-5")  # type: ignore[call-arg]
    graph = build_supervisor_graph(model=model)

    tenant_id = uuid4()
    run_id = uuid4()
    USER_INPUT = "Recover dormant customers from the last 60 days"

    wallclock_start = time.monotonic()
    result = graph.invoke(
        {
            "messages": [{"role": "user", "content": USER_INPUT}],
            "tenant_id": tenant_id,
            "run_id": run_id,
        }
    )
    wallclock_s = time.monotonic() - wallclock_start

    final_bundle = result.get("sales_recovery_context")

    diag = {
        "wallclock_s": wallclock_s,
        "active_agent": result.get("active_agent"),
        "campaign_plan_type": type(result.get("campaign_plan")).__name__,
        "specialist_call_count": len(_CountingClient.calls),
        "specialist_call_ledger": _CountingClient.calls,
        "result_keys": sorted(result.keys()) if result else None,
        "bundle_present": final_bundle is not None,
        "bundle_user_request": (
            final_bundle.user_request if final_bundle is not None else None
        ),
        "bundle_data_completeness": (
            dict(final_bundle.data_completeness)
            if final_bundle is not None
            else None
        ),
        "bundle_trigger_reason": (
            final_bundle.trigger_reason if final_bundle is not None else None
        ),
    }

    # --- Proof-of-call -------------------------------------------------
    assert len(_CountingClient.calls) >= 1, diag
    first_call = _CountingClient.calls[0]
    assert first_call["model"] == "claude-haiku-4-5", diag
    assert isinstance(first_call["response_id"], str), diag
    assert first_call["response_id"].startswith("msg_"), diag
    assert result.get("active_agent") == "sales_recovery_agent", diag
    # Haiku is faster than Opus; relax the wall-clock floor accordingly.
    assert wallclock_s > 0.5, diag

    # --- Wire-through (Exec-6.85) --------------------------------------
    assert final_bundle is not None, diag
    assert final_bundle.user_request == USER_INPUT, diag
    assert first_call["first_user_message"] == USER_INPUT, diag
    assert final_bundle.tenant_id == tenant_id, diag
    assert final_bundle.run_id == run_id, diag
    assert set(final_bundle.data_completeness.keys()) == {
        "business_profile",
        "customer_ledger_summary",
        "recent_campaigns",
        "attribution_snapshot",
        "pending_owner_inputs",
    }, diag

    # --- Verdict capture (no narrowing) --------------------------------
    plan = result.get("campaign_plan")
    assert isinstance(plan, _CampaignPlanVariants), diag
    assert plan.tenant_id == tenant_id, diag
    assert plan.run_id == run_id, diag
    # Surface the verdict + finding regardless of variant — captured so a
    # CI run output (or a manual paste in the PR body) carries the truth.
    print(
        "EXEC685_VERDICT:",
        type(plan).__name__,
        "data_completeness:",
        dict(final_bundle.data_completeness),
        "user_request_threaded:",
        final_bundle.user_request,
        "calls:",
        len(_CountingClient.calls),
        "wallclock_s:",
        round(wallclock_s, 2),
    )


# --- VT-3.4 PR 2/3: landmine-1 keyless precedence test (CL-202 / CL-203) ------


class ToolBindableFake(GenericFakeChatModel):
    """GenericFakeChatModel that survives ``create_agent``'s tool binding.

    GenericFakeChatModel inherits BaseChatModel.bind_tools, whose body is
    ``raise NotImplementedError`` — so ``create_agent``, which calls
    ``model.bind_tools(tools)`` to wire the orchestrator's tools, blows up on
    the raw fake (verified probe, CL-203).

    tool_calls are baked into the pre-canned AIMessages, so this fake never
    synthesises a tool call from a schema. ``bind_tools`` only needs to not
    raise, so ``create_agent`` can finish wiring; ``return self`` does that and
    preserves the GenericFakeChatModel invoke-iterator path. The landmine-1
    observation is determined by the langgraph executor, not the model layer —
    this amendment is upstream of the observation surface.
    """

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self


def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Compact final-state view — scalar fields only, no message dump."""
    return {
        "keys": sorted(state.keys()),
        "active_agent": state.get("active_agent"),
        "terminated_without_spawn": state.get("terminated_without_spawn"),
        "campaign_plan_present": state.get("campaign_plan") is not None,
        "message_count": len(state.get("messages", [])),
    }


def _run_supervisor_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned_messages: list[AIMessage],
    user_text: str,
) -> tuple[list[str], dict[str, Any], list[str], list[str]]:
    """Build + run the real supervisor graph with a ToolBindableFake.

    Wraps ``route_after_orchestrator`` to record whether — and with what key —
    the conditional edge's router actually fires. Returns
    ``(node_visit_trace, final_state, route_keys, captured_warnings)``.
    """
    import orchestrator.context_builder as context_builder_mod
    import orchestrator.supervisor as supervisor_mod

    # VT-138: _build_recent_campaigns now reads the live campaigns table via
    # tenant_connection. This landmine test runs keyless with no DB pool —
    # stub the builder back to safe-empty so spawn_sales_recovery's bundle
    # construction stays pure-Python. The DB read path itself is covered by
    # the substrate-fixture suite in test_context_builder_campaigns_readpath.py.
    monkeypatch.setattr(
        context_builder_mod, "_build_recent_campaigns", lambda tid: ([], False)
    )

    route_keys: list[str] = []
    real_route = routing.route_after_orchestrator

    def recording_route(state: Any) -> str:
        key: str = real_route(state)
        route_keys.append(key)
        return key

    # build_supervisor_graph reads route_after_orchestrator as a module global;
    # patch the supervisor module's binding before the graph is built.
    monkeypatch.setattr(supervisor_mod, "route_after_orchestrator", recording_route)

    # PR 3/3 added a `collapse` node downstream of sales_recovery_agent that
    # writes to Postgres via tenant_connection. This landmine test exercises
    # routing precedence with a fake model and no DB — neutralise the collapse
    # node so the spawn path does not hit `get_pool()`.
    monkeypatch.setattr(supervisor_mod, "collapse_node", lambda state: {})

    # Dispatch switch (VT-SR-Agent Exec Order 6.7): sales_recovery_agent now
    # calls run_sales_recovery_agent (real Anthropic Messages SDK + the
    # self-evaluate gate with a VT-50 adapter). This landmine test runs
    # keyless — neutralise the specialist node so the spawn path does not
    # construct Anthropic() / hit the API. Routing precedence is the
    # surface under test; the specialist's body is irrelevant here.
    monkeypatch.setattr(
        supervisor_mod, "_sales_recovery_node", lambda state: {}
    )

    trace: list[str] = []
    final_state: dict[str, Any] = {}
    # tenant_id / run_id: spawn_sales_recovery's handoff fail-loud-requires
    # run identity in state (CL-209). Seeded so the spawn path builds its
    # bundle instead of raising TenantIsolationError.
    initial = {
        "messages": [{"role": "user", "content": user_text}],
        "tenant_id": uuid4(),
        "run_id": uuid4(),
    }

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fake = ToolBindableFake(messages=iter(canned_messages))
        # fake is not a ChatAnthropic — intentional: only the model layer is
        # stubbed (CL-203). The graph wiring under test is real.
        graph = build_supervisor_graph(model=fake)
        for mode, chunk in graph.stream(initial, stream_mode=["updates", "values"]):
            if mode == "updates":
                trace.extend(chunk.keys())
            elif mode == "values":
                final_state = chunk

    return trace, final_state, route_keys, [str(w.message) for w in caught]


def test_supervisor_graph_spawn_vs_no_spawn_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Landmine 1 — Command.PARENT vs conditional-edge precedence (CL-202).

    The spawn tool returns ``Command(goto='sales_recovery_agent',
    graph=Command.PARENT)`` while ``add_conditional_edges`` ALSO sits after the
    orchestrator node. Context7 does not document which wins for this
    composition. This test runs both paths with a fake model and records the
    observed node-visit trace, the final state, whether the conditional-edge
    router fired, and any langgraph warnings.

    The assertions hold for BOTH possible precedence outcomes — they pin the
    destination guarantees only. The distinguishing detail (does
    orchestrator_terminal also run on the spawn path, does sales_recovery_agent
    run twice) is printed under LANDMINE1_ prefixes, NOT asserted.
    """
    # Spawn path: 1 spawn tool-call message + cushion content messages so a
    # double-fire (DIVERGENCE) yields a clean trace rather than a StopIteration
    # crash from the fake's exhausted iterator.
    spawn_messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "spawn_sales_recovery", "args": {}, "id": "1"}],
        ),
        AIMessage(content="stub specialist done (1)"),
        AIMessage(content="stub specialist done (2)"),
        AIMessage(content="stub specialist done (3)"),
    ]
    # No-spawn path: orchestrator produces a plain AIMessage, no tool_calls.
    no_spawn_messages = [
        AIMessage(content="Cannot help with that.", tool_calls=[]),
    ]

    s_trace, s_final, s_route, s_warn = _run_supervisor_path(
        monkeypatch,
        canned_messages=spawn_messages,
        user_text="Recover dormant customers from the last 60 days",
    )
    print("LANDMINE1_TRACE: spawn:", s_trace)
    print("LANDMINE1_FINAL_STATE: spawn:", _state_summary(s_final))
    print(
        "LANDMINE1_ROUTE_FN_INVOKED: spawn:",
        bool(s_route),
        "keys=",
        s_route,
    )
    print("LANDMINE1_WARNINGS: spawn:", s_warn)
    print(
        "LANDMINE1_NOTE: spawn orchestrator_terminal_visited=",
        "orchestrator_terminal" in s_trace,
        "sales_recovery_agent_visit_count=",
        s_trace.count("sales_recovery_agent"),
    )

    n_trace, n_final, n_route, n_warn = _run_supervisor_path(
        monkeypatch,
        canned_messages=no_spawn_messages,
        user_text="Just checking in, nothing for you to do today",
    )
    print("LANDMINE1_TRACE: no_spawn:", n_trace)
    print("LANDMINE1_FINAL_STATE: no_spawn:", _state_summary(n_final))
    print(
        "LANDMINE1_ROUTE_FN_INVOKED: no_spawn:",
        bool(n_route),
        "keys=",
        n_route,
    )
    print("LANDMINE1_WARNINGS: no_spawn:", n_warn)

    # Assertions — destination guarantees, true for either precedence outcome.
    assert "sales_recovery_agent" in s_trace, (
        "spawn path must reach sales_recovery_agent"
    )
    assert "orchestrator_terminal" in n_trace, (
        "no-spawn path must reach orchestrator_terminal"
    )
    assert "sales_recovery_agent" not in n_trace, (
        "no-spawn path must NOT reach sales_recovery_agent"
    )


# --- Exec-6.85: Context Composer bundle wire-through (keyless) ----------------


def test_sales_recovery_node_passes_bundle_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exec-6.85 seam: ``_sales_recovery_node`` consumes the Composer
    bundle from ``state['sales_recovery_context']`` and hands it to
    ``run_sales_recovery_agent`` unchanged. Locks against a regression
    where the node constructs a fresh minimal context and discards the
    bundle (the pre-Exec-6.85 defect).

    Keyless: monkeypatches ``run_sales_recovery_agent`` to capture the
    received context and short-circuit. SelfEvaluateAdapter constructs
    against a ToolContext with real UUIDs from the bundle.
    """
    import orchestrator.context_builder as context_builder_mod
    import orchestrator.supervisor as supervisor_mod
    from orchestrator.agent.types import AgentResult
    from orchestrator.context_builder import build_sales_recovery_context

    # VT-138: _build_recent_campaigns hits the live DB by default; stub it
    # back to safe-empty for this keyless wire-through test.
    monkeypatch.setattr(
        context_builder_mod, "_build_recent_campaigns", lambda tid: ([], False)
    )

    received: dict[str, Any] = {}

    def _capture(context: Any, *, evaluator: Any) -> AgentResult:
        received["context"] = context
        received["evaluator"] = evaluator
        return AgentResult(
            status="completed",
            output={
                "status": "insufficient_data",
                "tenant_id": str(context.tenant_id),
                "run_id": str(context.run_id),
                "generated_at": "2026-05-22T00:00:00+00:00",
                "missing_data": [
                    {
                        "category": "customer_ledger_summary",
                        "description": "no dormant customers seeded",
                        "suggested_remediation": "Run customer-ledger ingest.",
                    }
                ],
            },
        )

    monkeypatch.setattr(supervisor_mod, "run_sales_recovery_agent", _capture)
    # Avoid persisting to DB — the node's downstream parse_campaign_plan is
    # the only consumer of the AgentResult; that runs in-process.
    monkeypatch.setattr(
        supervisor_mod, "SelfEvaluateAdapter", lambda *, ctx: None
    )

    tenant_id = uuid4()
    run_id = uuid4()
    bundle = build_sales_recovery_context(
        tenant_id, run_id, "weekly_cadence", "Recover dormant customers"
    )

    update = supervisor_mod._sales_recovery_node(
        {"sales_recovery_context": bundle}
    )

    # Wire-through proof: the agent received the SAME bundle the handoff
    # attached — not a freshly constructed minimal context.
    assert received["context"] is bundle
    # Downstream parse still produces a plan keyed to bundle identity.
    plan = update["campaign_plan"]
    assert plan.tenant_id == tenant_id
    assert plan.run_id == run_id


def test_sales_recovery_node_fails_loud_on_missing_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exec-6.85: a None bundle at the seam is a broken handoff. Fail
    loud with TenantIsolationError rather than silently constructing a
    fresh context that would run the specialist against no task data."""
    import orchestrator.supervisor as supervisor_mod
    from orchestrator._tenant_guard import TenantIsolationError

    with pytest.raises(TenantIsolationError, match="sales_recovery_context"):
        supervisor_mod._sales_recovery_node({})
    with pytest.raises(TenantIsolationError, match="sales_recovery_context"):
        supervisor_mod._sales_recovery_node(
            {"sales_recovery_context": None}
        )


def test_spawn_sales_recovery_attaches_bundle_with_user_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exec-6.85: ``_build_sales_recovery_update`` reads the first user
    message from state and threads it into the bundle's ``user_request``.
    Pre-Exec-6.85 the bundle had no user_request field; the specialist
    extracted it node-side. Now the seam carries it.
    """
    import orchestrator.context_builder as context_builder_mod
    from orchestrator.handoffs import _build_sales_recovery_update

    # VT-138: stub the DB-backed campaigns builder; this keyless test
    # exercises spawn-time bundle assembly, not the live read path.
    monkeypatch.setattr(
        context_builder_mod, "_build_recent_campaigns", lambda tid: ([], False)
    )

    tenant_id = uuid4()
    run_id = uuid4()
    USER_TEXT = "Recover dormant customers from the last 60 days"

    update = _build_sales_recovery_update(
        {
            "messages": [{"role": "user", "content": USER_TEXT}],
            "tenant_id": tenant_id,
            "run_id": run_id,
            "trigger_reason": "owner_initiated",
        }
    )

    bundle = update["sales_recovery_context"]
    assert bundle.tenant_id == tenant_id
    assert bundle.run_id == run_id
    assert bundle.user_request == USER_TEXT
    assert bundle.trigger_reason == "owner_initiated"
