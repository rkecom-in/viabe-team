"""VT-101 Stage 3(a)+(b) — Sales-Recovery agent_framework routing cutover (behind the flag).

Proves the flag-ON paths are FAITHFUL to the direct paths WITHOUT hitting Anthropic/DB:

  - PROPOSER (``supervisor._sales_recovery_node``): flag-ON routes through the framework module and
    yields the SAME downstream (``result_output`` / ``result_status``) as the direct
    ``run_sales_recovery_agent`` — proven for a dict output (the exact object reaches
    ``parse_campaign_plan``) AND for ``output=None`` (the None -> ``SpecialistNoOutputError``
    detection still fires, status verbatim). Flag-OFF calls ``run_sales_recovery_agent`` directly
    and NEVER touches the module (``_sr_registered_module`` is not called).
  - EXECUTOR (``coordinator.get_registry``): flag-ON serves ``sales_recovery`` via a
    ``CoordinatorAgentAdapter`` (``name == 'sales_recovery'`` so ``_validate_registry`` passes);
    flag-OFF serves the hand-wired ``SalesRecoveryAgent``.

Dep discipline: importing ``supervisor`` / ``coordinator`` pulls langchain / langgraph / anthropic /
dbos at runtime; ``importorskip`` those so the dep-less smoke skips this module and the full suite
runs it.
"""

from __future__ import annotations

import types
from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")
pytest.importorskip("anthropic")
pytest.importorskip("dbos")

from orchestrator import supervisor  # noqa: E402
from orchestrator.agent_framework import AgentFrameworkRegistry  # noqa: E402
from orchestrator.agent_framework.modules.sales_recovery_module import (  # noqa: E402
    FRAMEWORK_ROUTING_FLAG,
    SalesRecoveryModule,
)
from orchestrator.agent_framework.registration import (  # noqa: E402
    CoordinatorAgentAdapter,
)
from orchestrator.agents import coordinator  # noqa: E402
from orchestrator.supervisor import SpecialistNoOutputError  # noqa: E402


# --- helpers -----------------------------------------------------------------------------------


def _isolate_node(monkeypatch):
    """Neutralize the node's real side-effecting collaborators so only the routing branch runs.

    - ``SelfEvaluateAdapter`` construction (would build an Anthropic client) -> a bare stub.
    - ``validate_context_isolation`` (VT-73 gate — a DB re-query) -> no-op. It STAYS on the path
      (unchanged by VT-101); we only stub its body to keep the unit test offline.
    """
    monkeypatch.setattr(supervisor, "SelfEvaluateAdapter", lambda ctx: object())
    monkeypatch.setattr(
        "orchestrator.context_validator.validate_context_isolation", lambda ctx: None
    )


def _state():
    """A minimal graph state carrying a pre-built SR context (IDs only, no DB)."""
    tenant_uuid = uuid4()
    run_uuid = uuid4()
    context = types.SimpleNamespace(tenant_id=tenant_uuid, run_id=run_uuid)
    return {"sales_recovery_context": context}, tenant_uuid, run_uuid


def _patch_parse_capture(monkeypatch, tenant_uuid, run_uuid, captured):
    """Patch ``parse_campaign_plan`` to record the exact output it received and return a plan whose
    ids match the context (so the node skips the override ``model_copy`` and returns cleanly)."""

    def fake_parse(output):
        captured["output"] = output
        return types.SimpleNamespace(tenant_id=tenant_uuid, run_id=run_uuid)

    monkeypatch.setattr(supervisor, "parse_campaign_plan", fake_parse)


def _forbid(name):
    def _boom(*args, **kwargs):
        raise AssertionError(f"{name} must not be called on this path")

    return _boom


# --- PROPOSER, flag ON -------------------------------------------------------------------------


def test_proposer_flag_on_routes_through_module_dict_output(monkeypatch):
    """Flag-ON dict path: the module's ``propose`` passes the EXACT output object downstream to
    ``parse_campaign_plan`` (byte-identical to the direct path), and ``run_sales_recovery_agent`` is
    never called directly."""
    _isolate_node(monkeypatch)
    monkeypatch.setenv(FRAMEWORK_ROUTING_FLAG, "1")

    state, tenant_uuid, run_uuid = _state()
    output_obj = {"campaign_plan": {"variant": "A"}}
    captured_ctx = {}

    def fake_proposer(context, *, evaluator):
        captured_ctx["context"] = context
        return types.SimpleNamespace(status="completed", output=output_obj, terminated_reason=None)

    registered = AgentFrameworkRegistry().register(SalesRecoveryModule(proposer=fake_proposer))
    monkeypatch.setattr(supervisor, "_sr_registered_module", lambda: registered)
    monkeypatch.setattr(supervisor, "run_sales_recovery_agent", _forbid("run_sales_recovery_agent"))

    parsed = {}
    _patch_parse_capture(monkeypatch, tenant_uuid, run_uuid, parsed)

    result = supervisor._sales_recovery_node(state)

    # the EXACT output object flowed through the module into parse_campaign_plan (verbatim).
    assert parsed["output"] is output_obj
    # the module received the pre-built context object from state.
    assert captured_ctx["context"] is state["sales_recovery_context"]
    # and the node returns the parsed plan (ids matched, so no override copy).
    assert result["campaign_plan"].tenant_id == tenant_uuid


def test_proposer_flag_on_none_output_still_raises(monkeypatch):
    """Flag-ON None path: the module is None-preserving, so ``result_output is None`` still fires the
    structured ``SpecialistNoOutputError`` with the terminal status carried through verbatim."""
    _isolate_node(monkeypatch)
    monkeypatch.setenv(FRAMEWORK_ROUTING_FLAG, "1")

    state, tenant_uuid, run_uuid = _state()

    def fake_proposer(context, *, evaluator):
        return types.SimpleNamespace(status="refused", output=None, terminated_reason=None)

    registered = AgentFrameworkRegistry().register(SalesRecoveryModule(proposer=fake_proposer))
    monkeypatch.setattr(supervisor, "_sr_registered_module", lambda: registered)
    monkeypatch.setattr(supervisor, "run_sales_recovery_agent", _forbid("run_sales_recovery_agent"))

    with pytest.raises(SpecialistNoOutputError) as excinfo:
        supervisor._sales_recovery_node(state)

    assert excinfo.value.status == "refused"
    assert excinfo.value.specialist == "sales_recovery"
    assert excinfo.value.tenant_id == tenant_uuid
    assert excinfo.value.run_id == run_uuid


# --- PROPOSER, flag OFF (byte-identical to pre-VT-101) -----------------------------------------


def test_proposer_flag_off_calls_agent_directly(monkeypatch):
    """Flag-OFF dict path: ``run_sales_recovery_agent`` is called directly, the module registration
    is NEVER reached, and the SAME exact output object reaches ``parse_campaign_plan``."""
    _isolate_node(monkeypatch)
    monkeypatch.delenv(FRAMEWORK_ROUTING_FLAG, raising=False)

    state, tenant_uuid, run_uuid = _state()
    output_obj = {"campaign_plan": {"variant": "B"}}

    called = {}

    def fake_agent(context, *, evaluator):
        called["ctx"] = context
        return types.SimpleNamespace(status="completed", output=output_obj)

    monkeypatch.setattr(supervisor, "run_sales_recovery_agent", fake_agent)
    monkeypatch.setattr(supervisor, "_sr_registered_module", _forbid("_sr_registered_module"))

    parsed = {}
    _patch_parse_capture(monkeypatch, tenant_uuid, run_uuid, parsed)

    result = supervisor._sales_recovery_node(state)

    assert called["ctx"] is state["sales_recovery_context"]
    assert parsed["output"] is output_obj  # identical downstream value to the flag-ON path
    assert result["campaign_plan"].tenant_id == tenant_uuid


def test_proposer_flag_off_none_output_raises(monkeypatch):
    """Flag-OFF None path: unchanged VT-492 behavior — ``SpecialistNoOutputError`` with status
    verbatim, module path not taken."""
    _isolate_node(monkeypatch)
    monkeypatch.delenv(FRAMEWORK_ROUTING_FLAG, raising=False)

    state, tenant_uuid, run_uuid = _state()

    monkeypatch.setattr(
        supervisor,
        "run_sales_recovery_agent",
        lambda context, *, evaluator: types.SimpleNamespace(status="invalid", output=None),
    )
    monkeypatch.setattr(supervisor, "_sr_registered_module", _forbid("_sr_registered_module"))

    with pytest.raises(SpecialistNoOutputError) as excinfo:
        supervisor._sales_recovery_node(state)

    assert excinfo.value.status == "invalid"


# --- EXECUTOR: coordinator.get_registry ---------------------------------------------------------


def test_coordinator_registry_flag_on_uses_adapter(monkeypatch):
    """Flag-ON: the ``sales_recovery`` executor entry is a ``CoordinatorAgentAdapter`` whose
    ``.name == 'sales_recovery'`` (so ``_validate_registry`` still passes)."""
    monkeypatch.setenv(FRAMEWORK_ROUTING_FLAG, "1")
    coordinator._registry_cache = None
    try:
        reg = coordinator.get_registry()
        entry = reg["sales_recovery"]
        assert isinstance(entry, CoordinatorAgentAdapter)
        assert entry.name == "sales_recovery"
    finally:
        coordinator._registry_cache = None


def test_coordinator_registry_flag_off_uses_specialist(monkeypatch):
    """Flag-OFF: the ``sales_recovery`` entry is the hand-wired ``SalesRecoveryAgent`` (unchanged)."""
    from orchestrator.agents.sales_recovery_executor import SalesRecoveryAgent

    monkeypatch.delenv(FRAMEWORK_ROUTING_FLAG, raising=False)
    coordinator._registry_cache = None
    try:
        reg = coordinator.get_registry()
        entry = reg["sales_recovery"]
        assert isinstance(entry, SalesRecoveryAgent)
        assert not isinstance(entry, CoordinatorAgentAdapter)
        assert entry.name == "sales_recovery"
    finally:
        coordinator._registry_cache = None
