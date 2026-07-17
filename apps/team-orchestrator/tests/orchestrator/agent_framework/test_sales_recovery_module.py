"""VT-659 — unit tests for the Sales-Recovery agent_framework MODULE (the thin dual-role adapter).

Proves the module CONFORMS to the framework contract (``assert_conforms`` — all 9 checks) and that
its two lanes DELEGATE to the existing SR proposer/executor and MAP their results onto the framework
value objects WITHOUT touching a real LLM or DB (the delegates are injected). Also pins the two
migration invariants the adapter must hold: the manifest name equals the coordinator
``SpecialistAgent`` name (VT-659 invariant #17), and the proposer lane is structurally
side-effect-free (invariant #18 / the trust boundary).

Dep discipline (mirrors ``tests/agent/test_agent_framework.py``): ``assert_conforms`` + ``register``
reach the deny-list guard (langchain) at RUNTIME, and ``execute`` / the coordinator adapter reach
``dbos``. The module itself is import-light (its delegates are lazy). We ``importorskip`` those heavy
seams so the dep-less smoke skips the module; the full suite runs all of it.
"""

from __future__ import annotations

import types
from uuid import uuid4

import pytest

# ``assert_conforms`` (name_registerable) + ``register`` pull the deny-list guard, which imports
# langchain via ``orchestrator.agent.__init__``. Skip the whole module in the dep-less smoke.
pytest.importorskip("langchain")

from orchestrator.agent_framework import (  # noqa: E402 — after the importorskip guard
    CHECK_NAMES,
    AgentFrameworkRegistry,
    AgentRole,
    Capability,
    CapabilityNotDeclared,
    ModuleContext,
    assert_conforms,
    check_module_conformance,
)
from orchestrator.agent_framework.modules.sales_recovery_module import (  # noqa: E402
    EVALUATOR_KEY,
    MODULE_NAME,
    SR_CONTEXT_KEY,
    SalesRecoveryModule,
)

# The NON-GATED capability set the module declares (Option A — "arm != send").
_EXPECTED_CAPS = frozenset(
    {Capability.READ_CUSTOMER_LEDGER, Capability.PROPOSE_CAMPAIGN}
)


# --- test doubles ------------------------------------------------------------------------------


def _fake_agent_result(*, status="completed", output=None, terminated_reason=None):
    """A duck-typed ``agent.types.AgentResult`` — only the fields ``propose`` reads."""
    return types.SimpleNamespace(
        status=status,
        output=output if output is not None else {"campaign_plan": {"variant": "A"}},
        terminated_reason=terminated_reason,
    )


# --- 1. conformance (the required gate) --------------------------------------------------------


def test_module_conforms():
    """``assert_conforms`` passes — every trust-boundary property the framework depends on holds."""
    report = assert_conforms(SalesRecoveryModule())
    assert report.passed, str(report)
    # Full-coverage: the report carries every named check, and each one passed.
    assert {r.name for r in report.results} == set(CHECK_NAMES)
    assert all(r.passed for r in report.results), str(report)


def test_conformance_report_names_stable():
    """The report shape is stable (all 9 named checks present), via the pure entrypoint."""
    report = check_module_conformance(SalesRecoveryModule())
    assert [r.name for r in report.results] == list(CHECK_NAMES)
    # VT-669 added the 9th check (``required_tools_reachable``) to the suite.
    assert len(CHECK_NAMES) == 9
    assert "required_tools_reachable" in CHECK_NAMES


# --- 2. manifest shape (Option A capability model) ---------------------------------------------


def test_manifest_shape():
    m = SalesRecoveryModule.manifest
    assert m.name == MODULE_NAME == "sales_recovery"
    assert m.version == "1.0.0"
    assert m.roles == frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR})
    assert m.capabilities == _EXPECTED_CAPS
    # Option A: NO gated capability — the executor arms via the existing deterministic path.
    assert m.gated_capabilities == frozenset()
    assert Capability.REQUEST_CUSTOMER_SEND not in m.capabilities
    assert m.tools == ()
    # VT-669 SUFFICIENCY: SR holds NO tools of its own (tools=()), but its job REQUIRES the two
    # Manager-scoped common READ tools to frame a win-back — recorded here (the ``tools=()``
    # resolution: required reads are Manager-scoped, not tools on SR's own surface). Its send EFFECT
    # is NOT a required gated tool (arm != send, Option A) — the send is reached downstream via the
    # deterministic arm path, so there is no REQUEST_CUSTOMER_SEND tool to require.
    assert m.required_tools == ("read_customer_ledger_summary", "read_business_context")


def test_manifest_reuses_sr_activation_bar_verbatim():
    """The manifest carries SR's EXISTING activation bar (the single source; VT-421), unchanged."""
    from orchestrator.agents.activation_registry import REGISTRY

    m = SalesRecoveryModule.manifest
    assert m.prerequisites is REGISTRY["sales_recovery"]
    assert m.prerequisites.agent == m.name  # the validate() invariant the reuse must satisfy


def test_name_matches_coordinator_specialist():
    """VT-659 invariant #17: manifest.name == the coordinator SpecialistAgent name for SR (the
    ``CoordinatorAgentAdapter`` requires key == name == manifest.name at cutover)."""
    pytest.importorskip("dbos")
    from orchestrator.agents.coordinator import _REGISTRY_SPEC
    from orchestrator.agents.sales_recovery_executor import (
        AGENT_NAME,
        SalesRecoveryAgent,
    )

    assert SalesRecoveryModule.manifest.name == AGENT_NAME == "sales_recovery"
    assert SalesRecoveryAgent.name == SalesRecoveryModule.manifest.name
    assert "sales_recovery" in _REGISTRY_SPEC


# --- 3. PROPOSER lane: delegate + map + pure -----------------------------------------------------


def test_propose_delegates_and_maps():
    """``propose`` hands the pre-built context (+ evaluator) to the SR proposer and maps its
    ``AgentResult`` -> ``ModuleResult(role=PROPOSER, proposal=output)``. Round-trips to AgentResult.

    VT-101: ``proposal`` is the EXACT output object (verbatim, not a copy) — the live proposer node
    reads it directly (never through the lossy ``to_agent_result``)."""
    captured = {}
    output_obj = {"campaign_plan": {"variant": "A"}}

    def fake_proposer(context, *, evaluator):
        captured["context"] = context
        captured["evaluator"] = evaluator
        return _fake_agent_result(output=output_obj)

    sentinel_ctx = object()
    module = SalesRecoveryModule(proposer=fake_proposer)
    registered = AgentFrameworkRegistry().register(module)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()),
        module_name=MODULE_NAME,
        data={SR_CONTEXT_KEY: sentinel_ctx},
    )

    result = registered.run(ctx)  # dispatches to propose() by ctx.role

    assert result.role is AgentRole.PROPOSER
    assert result.status == "completed"
    # verbatim passthrough — the exact object, not a rebuilt dict (None-preserving fix).
    assert result.proposal is output_obj
    # the exact pre-built context object was delegated, evaluator defaults to None (VT-36 skipped).
    assert captured["context"] is sentinel_ctx
    assert captured["evaluator"] is None

    # generalization proof: the proposal maps back onto the existing AgentResult envelope.
    agent_result = result.to_agent_result()
    assert agent_result.status == "completed"
    assert agent_result.output == {"campaign_plan": {"variant": "A"}}


def test_propose_preserves_none_output():
    """VT-101 money-path faithfulness: an ``AgentResult`` with ``output=None`` maps to
    ``ModuleResult.proposal IS None`` (NOT ``{}``), with ``status`` verbatim. The live proposer node
    relies on ``proposal is None`` to fire ``SpecialistNoOutputError``; the old ``dict(output or {})``
    collapsed None -> {} and would have masked the no-output terminal."""

    def fake_proposer(context, *, evaluator):
        return types.SimpleNamespace(
            status="refused", output=None, terminated_reason="no lapsed customers"
        )

    module = SalesRecoveryModule(proposer=fake_proposer)
    registered = AgentFrameworkRegistry().register(module)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()),
        module_name=MODULE_NAME,
        data={SR_CONTEXT_KEY: object()},
    )

    result = registered.run(ctx)

    assert result.role is AgentRole.PROPOSER
    assert result.proposal is None
    assert result.status == "refused"
    assert result.reason == "no lapsed customers"


def test_propose_passes_evaluator_when_supplied():
    """A manager may opt a run into the VT-36 self-evaluate gate via ``ctx.data[EVALUATOR_KEY]``."""
    captured = {}

    def fake_proposer(context, *, evaluator):
        captured["evaluator"] = evaluator
        return _fake_agent_result()

    evaluator_sentinel = object()
    module = SalesRecoveryModule(proposer=fake_proposer)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()),
        module_name=MODULE_NAME,
        data={SR_CONTEXT_KEY: object(), EVALUATOR_KEY: evaluator_sentinel},
    )
    module.propose(ctx, AgentFrameworkRegistry().register(module).new_gate(ctx))
    assert captured["evaluator"] is evaluator_sentinel


def test_propose_missing_context_fails_loud():
    """No pre-built SalesRecoveryContext in ctx.data -> fail-closed ValueError (a wiring bug)."""
    module = SalesRecoveryModule(proposer=lambda *a, **k: _fake_agent_result())
    registered = AgentFrameworkRegistry().register(module)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()), module_name=MODULE_NAME
    )  # no data
    with pytest.raises(ValueError, match=SR_CONTEXT_KEY):
        registered.run(ctx)


def test_propose_maps_terminated_reason():
    """A terminated run carries its reason across into ModuleResult.reason (diagnostics)."""
    module = SalesRecoveryModule(
        proposer=lambda *a, **k: _fake_agent_result(
            status="terminated", output={}, terminated_reason="wallclock exceeded 300s budget"
        )
    )
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()),
        module_name=MODULE_NAME,
        data={SR_CONTEXT_KEY: object()},
    )
    result = module.propose(ctx, AgentFrameworkRegistry().register(module).new_gate(ctx))
    assert result.status == "terminated"
    assert result.reason == "wallclock exceeded 300s budget"


def test_proposer_lane_is_structurally_readonly():
    """The proposer-lane facade is EMPTY (no gated cap) — a send attempt raises. Holds even though
    the SAME module is a dual-role executor: ``capabilities_for_role`` strips gated caps (there are
    none here) and the module declares no send capability at all (Option A)."""
    module = SalesRecoveryModule()
    registered = AgentFrameworkRegistry().register(module)
    p_ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()),
        module_name=MODULE_NAME,
        data={SR_CONTEXT_KEY: object()},
    )
    gate = registered.new_gate(p_ctx)
    assert gate.capabilities == _EXPECTED_CAPS
    assert gate.can(Capability.REQUEST_CUSTOMER_SEND) is False
    with pytest.raises(CapabilityNotDeclared):
        gate.request_customer_send("draft-1")


# --- 4. EXECUTOR lane: delegate + map ------------------------------------------------------------


def _fake_executor(capture: dict, *, work_item_status="awaiting_approval", batch_id="batch-1",
                   counters=None):
    from orchestrator.agents.coordinator import ItemExecutionResult

    def execute_item(item_ctx):
        capture["item_ctx"] = item_ctx
        return ItemExecutionResult(
            work_item_status=work_item_status,
            batch_id=batch_id,
            counters=dict(counters or {"drafted": 2}),
        )

    return types.SimpleNamespace(execute_item=execute_item)


def test_execute_delegates_and_maps():
    """``execute`` builds a coordinator ``AgentItemContext`` from the ModuleContext, delegates to
    ``execute_item``, and maps ``ItemExecutionResult`` -> ``ModuleResult(role=EXECUTOR, ...)``.
    Round-trips back to ItemExecutionResult."""
    pytest.importorskip("dbos")
    capture = {}
    module = SalesRecoveryModule(executor_factory=lambda: _fake_executor(capture))
    registered = AgentFrameworkRegistry().register(module)

    tenant = uuid4()
    ctx = ModuleContext.for_executor(
        tenant_id=tenant, item_id="item-9", work_item_id="wi-9", run_id="run-9"
    )
    result = registered.run(ctx)  # dispatches to execute() by ctx.role

    assert result.role is AgentRole.EXECUTOR
    assert result.status == "awaiting_approval"
    assert result.work_item_status == "awaiting_approval"
    assert result.batch_id == "batch-1"
    assert result.counters == {"drafted": 2}

    # the ModuleContext was translated into the coordinator's AgentItemContext, IDs intact.
    ic = capture["item_ctx"]
    assert ic.tenant_id == str(tenant)
    assert ic.item_id == "item-9"
    assert ic.work_item_id == "wi-9"
    assert ic.run_id == "run-9"
    assert ic.agent == "sales_recovery"

    # generalization proof: maps back onto the existing ItemExecutionResult.
    ier = result.to_item_execution_result()
    assert ier.work_item_status == "awaiting_approval"
    assert ier.batch_id == "batch-1"
    assert ier.counters == {"drafted": 2}


def test_execute_maps_cancelled_outcome():
    """A cancelled executor outcome (e.g. consent gate) maps through unchanged — no batch_id."""
    pytest.importorskip("dbos")
    capture = {}
    module = SalesRecoveryModule(
        executor_factory=lambda: _fake_executor(
            capture, work_item_status="cancelled", batch_id=None, counters={"skipped_owner_inputs": 1}
        )
    )
    ctx = ModuleContext.for_executor(
        tenant_id=uuid4(), item_id="i", work_item_id="w", run_id="r"
    )
    result = module.execute(ctx, AgentFrameworkRegistry().register(module).new_gate(ctx))
    assert result.status == "cancelled"
    assert result.batch_id is None
    assert result.counters == {"skipped_owner_inputs": 1}


# --- 5. coordinator seam: the executor adapts to the SpecialistAgent Protocol -------------------


def test_coordinator_adapter_roundtrip():
    """VT-659 invariant #17: the registered EXECUTOR wraps into a ``CoordinatorAgentAdapter`` that
    satisfies the coordinator ``SpecialistAgent`` Protocol with ``name == 'sales_recovery'``."""
    pytest.importorskip("dbos")
    from orchestrator.agent_framework.registration import CoordinatorAgentAdapter
    from orchestrator.agents.coordinator import (
        AgentItemContext,
        ItemExecutionResult,
        SpecialistAgent,
    )

    capture = {}
    module = SalesRecoveryModule(executor_factory=lambda: _fake_executor(capture))
    registered = AgentFrameworkRegistry().register(module)
    adapter = CoordinatorAgentAdapter(registered)

    assert adapter.name == "sales_recovery"
    assert isinstance(adapter, SpecialistAgent)  # runtime_checkable Protocol conformance

    tenant = uuid4()
    item = AgentItemContext(
        tenant_id=str(tenant),
        item_id="item-1",
        agent="sales_recovery",
        work_item_id="wi-1",
        run_id="run-1",
    )
    out = adapter.execute_item(item)
    assert isinstance(out, ItemExecutionResult)
    assert out.work_item_status == "awaiting_approval"
    assert out.counters == {"drafted": 2}
    assert capture["item_ctx"].tenant_id == str(tenant)


# --- 6. dual-role dispatch: ONE instance, both lanes --------------------------------------------


def test_dual_role_registers_once_and_both_lanes_dispatch():
    """The Sales-Recovery shape: a single registration serves BOTH roles; ``run`` selects the method
    by ``ctx.role`` on the SAME instance."""
    pytest.importorskip("dbos")
    capture = {}
    module = SalesRecoveryModule(
        proposer=lambda context, *, evaluator: _fake_agent_result(output={"lane": "propose"}),
        executor_factory=lambda: _fake_executor(capture),
    )
    reg = AgentFrameworkRegistry()
    registered = reg.register(module)
    assert reg.names() == ["sales_recovery"]  # ONE registration for BOTH roles

    tenant = uuid4()
    p_out = registered.run(
        ModuleContext.for_proposer(
            tenant_model_value=str(tenant),
            module_name=MODULE_NAME,
            data={SR_CONTEXT_KEY: object()},
        )
    )
    assert p_out.role is AgentRole.PROPOSER
    assert p_out.proposal == {"lane": "propose"}

    e_out = registered.run(
        ModuleContext.for_executor(
            tenant_id=tenant, item_id="i", work_item_id="w", run_id="r"
        )
    )
    assert e_out.role is AgentRole.EXECUTOR
    assert e_out.status == "awaiting_approval"
