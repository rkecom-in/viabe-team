"""Track-B — unit tests for the modular agent-integration framework.

Covers the four required guarantees + the reference plugin end-to-end:
  1. registration (a valid module registers; role/impl + name-uniqueness enforced);
  2. capability enforcement (a module declaring a forbidden capability is REJECTED at registration —
     both layers: the positive-capability rule AND the reused ``assert_agent_tools_safe`` deny-list);
  3. the gate facade DENIES a direct-send attempt (a proposer's empty facade raises), and ROUTES a
     declared gated action through the existing gate (never a direct transport);
  4. context isolation (the tenant IDOR guard holds — a model-supplied foreign tenant is ignored
     when an ambient dispatch context is present).

The framework core is import-light (no heavy deps at module top); tests that reach a heavy seam
(coordinator's ``dbos`` import) ``importorskip`` it, per the repo's dep-less-smoke discipline.
"""

from __future__ import annotations

import sys
import types
from uuid import uuid4

import pytest

# The framework PACKAGE is import-light (registration.py lazy-imports the deny-list guard), but these
# tests exercise register() and the coordinator adapter, which reach heavy seams at RUNTIME — langchain
# (via orchestrator.agent.__init__ → the deny-list guard) and dbos (the coordinator). Skip the whole
# module in the dep-less smoke; the full suite (deps present) runs all of it. Per the repo dep-less discipline.
pytest.importorskip("langchain")

from orchestrator.agent_framework import (  # noqa: E402 — after the importorskip guard
    AgentFrameworkRegistry,
    AgentManifest,
    AgentRole,
    Capability,
    CapabilityNotDeclared,
    GateFacade,
    ModuleContext,
    ModuleRegistrationError,
    ModuleResult,
)
from orchestrator.agent_framework.manifest import ManifestError
from orchestrator.agent_framework.reference_plugin import BusinessContextReader


# --- test doubles ------------------------------------------------------------------------------


class _ForbiddenToolProposer:
    """A proposer whose tool surface holds a direct customer-send tool — must be rejected."""

    manifest = AgentManifest(
        name="forbidden_tool_proposer",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="holds a send tool it must not have",
        capabilities=frozenset({Capability.READ_BUSINESS_CONTEXT}),
        tools=(types.SimpleNamespace(name="send_whatsapp_message"),),
    )

    def propose(self, ctx, gate):  # pragma: no cover - never reached (rejected at registration)
        raise AssertionError("must not register")


class _SendingExecutor:
    """A minimal EXECUTOR that declares REQUEST_CUSTOMER_SEND and asks the facade to send."""

    manifest = AgentManifest(
        name="sending_executor",
        version="1.0.0",
        roles=frozenset({AgentRole.EXECUTOR}),
        description="executor that arms a send through the facade",
        capabilities=frozenset({Capability.REQUEST_CUSTOMER_SEND}),
    )

    def __init__(self) -> None:
        self.sent = None

    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        self.sent = gate.request_customer_send("draft-123", autonomy_level="L2")
        return ModuleResult(role=AgentRole.EXECUTOR, status="sent")


def _fake_business_context(objective=None, identity=None):
    return types.SimpleNamespace(
        objective=objective or {"goal": "grow online orders"},
        identity=identity or {"name": "Test Biz"},
    )


# --- 1. registration ---------------------------------------------------------------------------


def test_register_valid_proposer():
    reg = AgentFrameworkRegistry()
    registered = reg.register(BusinessContextReader())
    assert registered.manifest.name == "business_context_reader"
    assert reg.names() == ["business_context_reader"]
    assert "business_context_reader" in reg
    assert reg.get("business_context_reader") is registered


def test_register_rejects_missing_manifest():
    reg = AgentFrameworkRegistry()
    with pytest.raises(ModuleRegistrationError):
        reg.register(object())


def test_register_rejects_role_impl_mismatch():
    """A PROPOSER manifest whose impl exposes no ``propose`` is rejected."""

    class _Broken:
        manifest = AgentManifest(
            name="broken",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="no propose method",
            capabilities=frozenset(),
        )
        # note: exposes execute, not propose

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    reg = AgentFrameworkRegistry()
    with pytest.raises(ModuleRegistrationError):
        reg.register(_Broken())


def test_register_rejects_duplicate_name():
    reg = AgentFrameworkRegistry()
    reg.register(BusinessContextReader())
    with pytest.raises(ModuleRegistrationError):
        reg.register(BusinessContextReader())


# --- 2. capability enforcement (forbidden capability rejected at registration) -----------------


def test_manifest_rejects_proposer_with_gated_capability():
    """Positive-capability rule: a PROPOSER may declare no gated (REQUEST_*) capability."""
    manifest = AgentManifest(
        name="bad_proposer",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="proposer illegally declaring a send capability",
        capabilities=frozenset({Capability.REQUEST_CUSTOMER_SEND}),
    )
    with pytest.raises(ManifestError):
        manifest.validate()


def test_registration_rejects_proposer_with_gated_capability():
    """The same rule enforced through the registration surface."""

    class _BadProposer:
        manifest = AgentManifest(
            name="bad_proposer2",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="x",
            capabilities=frozenset({Capability.REQUEST_BUSINESS_ACTION}),
        )

        def propose(self, ctx, gate):  # pragma: no cover
            ...

    reg = AgentFrameworkRegistry()
    with pytest.raises(ModuleRegistrationError):
        reg.register(_BadProposer())


def test_registration_rejects_forbidden_tool_surface():
    """Deny-list layer: a module HOLDING a forbidden send tool is rejected (reuses
    ``assert_agent_tools_safe``)."""
    reg = AgentFrameworkRegistry()
    with pytest.raises(ModuleRegistrationError):
        reg.register(_ForbiddenToolProposer())


@pytest.mark.parametrize(
    "bad_tool_name",
    ["send_whatsapp_template", "write_ledger_entry", "append_to_sheet", "execute_spend"],
)
def test_registration_rejects_various_forbidden_tools(bad_tool_name):
    class _M:
        manifest = AgentManifest(
            name=f"m_{bad_tool_name}",
            version="1.0.0",
            roles=frozenset({AgentRole.EXECUTOR}),
            description="x",
            capabilities=frozenset(),
            tools=(types.SimpleNamespace(name=bad_tool_name),),
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    reg = AgentFrameworkRegistry()
    with pytest.raises(ModuleRegistrationError):
        reg.register(_M())


def test_manifest_prereq_name_must_match():
    from orchestrator.agents.activation_registry import AgentPrerequisites

    manifest = AgentManifest(
        name="agent_a",
        version="1.0.0",
        roles=frozenset({AgentRole.EXECUTOR}),
        description="x",
        capabilities=frozenset(),
        prerequisites=AgentPrerequisites(agent="agent_b"),  # mismatch
    )
    with pytest.raises(ManifestError):
        manifest.validate()


# --- 3. gate facade: denies undeclared, routes declared ----------------------------------------


def test_gate_facade_denies_undeclared_customer_send():
    """A proposer's facade has an EMPTY gated set — request_customer_send raises."""
    facade = GateFacade(tenant_id=uuid4(), capabilities=frozenset({Capability.READ_BUSINESS_CONTEXT}))
    assert facade.can(Capability.REQUEST_CUSTOMER_SEND) is False
    with pytest.raises(CapabilityNotDeclared):
        facade.request_customer_send("draft-1")


def test_gate_facade_denies_undeclared_business_action():
    facade = GateFacade(tenant_id=uuid4(), capabilities=frozenset())
    with pytest.raises(CapabilityNotDeclared):
        facade.gate_business_action("SPEND", 1000)


def test_gate_facade_routes_declared_send_through_existing_gate(monkeypatch):
    """A declared REQUEST_CUSTOMER_SEND routes to customer_send.agent_send_draft with the facade's
    pinned tenant — proving the facade calls the EXISTING gate, never a direct transport.

    Injects a fake ``customer_send`` module via sys.modules so the heavy real module is not imported;
    the facade's lazy ``from orchestrator.agents.customer_send import agent_send_draft`` picks it up.
    """
    calls = {}

    fake = types.ModuleType("orchestrator.agents.customer_send")

    def agent_send_draft(tenant_id, draft_id, *, autonomy_level="L2", conn=None, send_fn=None):
        calls["tenant_id"] = tenant_id
        calls["draft_id"] = draft_id
        calls["autonomy_level"] = autonomy_level
        return "GATED_SEND_RESULT"

    fake.agent_send_draft = agent_send_draft
    monkeypatch.setitem(sys.modules, "orchestrator.agents.customer_send", fake)

    tenant = uuid4()
    facade = GateFacade(
        tenant_id=tenant, capabilities=frozenset({Capability.REQUEST_CUSTOMER_SEND})
    )
    result = facade.request_customer_send("draft-xyz", autonomy_level="L3")

    assert result == "GATED_SEND_RESULT"
    assert calls["tenant_id"] == tenant  # facade pins the tenant — module cannot override it
    assert calls["draft_id"] == "draft-xyz"
    assert calls["autonomy_level"] == "L3"


def test_gate_facade_routes_declared_business_action(monkeypatch):
    calls = {}
    fake = types.ModuleType("orchestrator.agents.business_impact_choke")

    def assert_or_gate_business_action(
        tenant_id, action_class, magnitude_minor, *, action_attrs=None, conn=None
    ):
        calls["tenant_id"] = tenant_id
        calls["action_class"] = action_class
        calls["magnitude_minor"] = magnitude_minor
        return "GATE_DECISION"

    fake.assert_or_gate_business_action = assert_or_gate_business_action
    monkeypatch.setitem(sys.modules, "orchestrator.agents.business_impact_choke", fake)

    tenant = uuid4()
    facade = GateFacade(
        tenant_id=tenant, capabilities=frozenset({Capability.REQUEST_BUSINESS_ACTION})
    )
    result = facade.gate_business_action("SPEND", 50000)
    assert result == "GATE_DECISION"
    assert calls["tenant_id"] == tenant
    assert calls["magnitude_minor"] == 50000


def test_executor_arms_send_only_through_facade(monkeypatch):
    """An EXECUTOR module reaches the send ONLY through the facade the framework hands it."""
    calls = {}
    fake = types.ModuleType("orchestrator.agents.customer_send")

    def agent_send_draft(tenant_id, draft_id, *, autonomy_level="L2", conn=None, send_fn=None):
        calls["tenant_id"] = tenant_id
        return "SENT"

    fake.agent_send_draft = agent_send_draft
    monkeypatch.setitem(sys.modules, "orchestrator.agents.customer_send", fake)

    reg = AgentFrameworkRegistry()
    registered = reg.register(_SendingExecutor())
    tenant = uuid4()
    ctx = ModuleContext.for_executor(
        tenant_id=tenant, item_id="item-1", work_item_id="wi-1", run_id="run-1"
    )
    result = registered.run(ctx)
    assert result.status == "sent"
    assert calls["tenant_id"] == tenant


# --- 4. context isolation: the tenant IDOR guard holds -----------------------------------------


def test_proposer_context_idor_guard_context_wins():
    """When an ambient dispatch context is present, a model-supplied FOREIGN tenant is ignored —
    the context tenant is authoritative (the VT-293/294/599 IDOR guard)."""
    from orchestrator.observability.decorators import (
        ObservabilityContext,
        _observability_context,
    )

    authoritative = uuid4()
    foreign = uuid4()
    token = _observability_context.set(
        ObservabilityContext(run_id=uuid4(), tenant_id=authoritative)
    )
    try:
        ctx = ModuleContext.for_proposer(
            tenant_model_value=str(foreign), module_name="business_context_reader"
        )
        assert ctx.tenant_id == authoritative
        assert ctx.tenant_id != foreign
    finally:
        _observability_context.reset(token)


def test_proposer_context_no_ambient_parses_model_value():
    """With no ambient context (a direct/unit call), a parseable tenant value is used."""
    from orchestrator.observability.decorators import _observability_context

    # Ensure no ambient context leaks from another test.
    assert _observability_context.get() is None
    tenant = uuid4()
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(tenant), module_name="business_context_reader"
    )
    assert ctx.tenant_id == tenant


def test_proposer_context_unresolvable_tenant_fails_closed():
    from orchestrator.agent_framework import TenantResolutionError
    from orchestrator.observability.decorators import _observability_context

    assert _observability_context.get() is None
    with pytest.raises(TenantResolutionError):
        ModuleContext.for_proposer(tenant_model_value=None, module_name="x")


def test_executor_context_bad_tenant_fails_closed():
    from orchestrator.agent_framework import TenantResolutionError

    with pytest.raises(TenantResolutionError):
        ModuleContext.for_executor(
            tenant_id="not-a-uuid", item_id="i", work_item_id="w", run_id="r"
        )


# --- 5. reference plugin end-to-end ------------------------------------------------------------


def test_reference_plugin_end_to_end():
    """registration -> capability declaration -> context in -> proposal out, no side effect."""
    reg = AgentFrameworkRegistry()
    plugin = BusinessContextReader(reader=lambda _tid: _fake_business_context())
    registered = reg.register(plugin)

    tenant = uuid4()
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(tenant), module_name="business_context_reader"
    )
    result = registered.run(ctx)

    assert result.role is AgentRole.PROPOSER
    assert result.status == "completed"
    assert result.proposal == {
        "objective": {"goal": "grow online orders"},
        "identity": {"name": "Test Biz"},
    }


def test_reference_plugin_cannot_send():
    """The reference proposer's facade is empty — a send attempt raises (structural read-only)."""
    reg = AgentFrameworkRegistry()
    registered = reg.register(BusinessContextReader(reader=lambda _tid: _fake_business_context()))
    tenant = uuid4()
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(tenant), module_name="business_context_reader"
    )
    gate = registered.new_gate(ctx)
    assert gate.capabilities == frozenset({Capability.READ_BUSINESS_CONTEXT})
    with pytest.raises(CapabilityNotDeclared):
        gate.request_customer_send("draft-1")


def test_reference_plugin_read_miss_yields_empty_proposal():
    def _boom(_tid):
        raise RuntimeError("db down")

    reg = AgentFrameworkRegistry()
    registered = reg.register(BusinessContextReader(reader=_boom))
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()), module_name="business_context_reader"
    )
    result = registered.run(ctx)
    assert result.proposal == {"objective": {}, "identity": {}}
    assert result.reason == "context_read_miss"


# --- 6. generalization proofs (adapters into the existing seams) -------------------------------


def test_proposer_result_adapts_to_agent_result():
    result = ModuleResult(role=AgentRole.PROPOSER, status="completed", proposal={"k": "v"})
    agent_result = result.to_agent_result()
    assert agent_result.status == "completed"
    assert agent_result.output == {"k": "v"}


def test_executor_adapter_conforms_to_coordinator_protocol():
    """Concrete generalization proof: a framework EXECUTOR module, wrapped by
    CoordinatorAgentAdapter, satisfies the coordinator's SpecialistAgent Protocol and returns an
    ItemExecutionResult. Guarded on dbos (coordinator imports it)."""
    pytest.importorskip("dbos")
    from orchestrator.agent_framework.registration import CoordinatorAgentAdapter
    from orchestrator.agents.coordinator import (
        AgentItemContext,
        ItemExecutionResult,
        SpecialistAgent,
    )

    class _Executor:
        manifest = AgentManifest(
            name="proto_executor",
            version="1.0.0",
            roles=frozenset({AgentRole.EXECUTOR}),
            description="x",
            capabilities=frozenset(),
        )

        def execute(self, ctx, gate):
            return ModuleResult(
                role=AgentRole.EXECUTOR,
                status="sent",
                work_item_status="sent",
                counters={"drafted": 1},
            )

    reg = AgentFrameworkRegistry()
    registered = reg.register(_Executor())
    adapter = CoordinatorAgentAdapter(registered)

    assert adapter.name == "proto_executor"
    assert isinstance(adapter, SpecialistAgent)  # runtime_checkable Protocol conformance

    tenant = uuid4()
    item_ctx = AgentItemContext(
        tenant_id=str(tenant),
        item_id="item-1",
        agent="proto_executor",
        work_item_id="wi-1",
        run_id="run-1",
    )
    out = adapter.execute_item(item_ctx)
    assert isinstance(out, ItemExecutionResult)
    assert out.work_item_status == "sent"
    assert out.counters == {"drafted": 1}


def test_coordinator_adapter_rejects_proposer():
    pytest.importorskip("dbos")
    from orchestrator.agent_framework.registration import CoordinatorAgentAdapter

    reg = AgentFrameworkRegistry()
    registered = reg.register(BusinessContextReader(reader=lambda _t: _fake_business_context()))
    with pytest.raises(ModuleRegistrationError):
        CoordinatorAgentAdapter(registered)


# --- 7. dual-role modules (Ruling #1: SR is ONE module = PROPOSER + EXECUTOR) -------------------


class _DualRoleModule:
    """One module declaring BOTH roles — proposes in the conversational lane AND executes a
    coordinator work item (the Sales-Recovery shape). Declares a gated capability: LEGAL because
    EXECUTOR is a declared role. Registered ONCE; dispatch is by ``ctx.role``."""

    manifest = AgentManifest(
        name="dual_role_module",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),
        description="proposes in the conversational lane AND executes a work item",
        capabilities=frozenset(
            {Capability.READ_BUSINESS_CONTEXT, Capability.REQUEST_CUSTOMER_SEND}
        ),
    )

    def propose(self, ctx, gate):
        return ModuleResult(
            role=AgentRole.PROPOSER, status="completed", proposal={"lane": "propose"}
        )

    def execute(self, ctx, gate):
        gate.request_customer_send("draft-dual", autonomy_level="L2")
        return ModuleResult(role=AgentRole.EXECUTOR, status="sent", work_item_status="sent")


def test_dual_role_registers_once_and_both_lanes_dispatch(monkeypatch):
    """A {PROPOSER, EXECUTOR} module registers ONCE; ``run`` dispatches by ``ctx.role`` — ``propose``
    under a proposer context, ``execute`` under an executor context — on the SAME instance."""
    calls = {}
    fake = types.ModuleType("orchestrator.agents.customer_send")

    def agent_send_draft(tenant_id, draft_id, *, autonomy_level="L2", conn=None, send_fn=None):
        calls["draft_id"] = draft_id
        return "SENT"

    fake.agent_send_draft = agent_send_draft
    monkeypatch.setitem(sys.modules, "orchestrator.agents.customer_send", fake)

    reg = AgentFrameworkRegistry()
    registered = reg.register(_DualRoleModule())
    assert reg.names() == ["dual_role_module"]  # ONE registration for BOTH roles

    tenant = uuid4()
    p_out = registered.run(
        ModuleContext.for_proposer(
            tenant_model_value=str(tenant), module_name="dual_role_module"
        )
    )
    assert p_out.role is AgentRole.PROPOSER
    assert p_out.proposal == {"lane": "propose"}

    e_out = registered.run(
        ModuleContext.for_executor(
            tenant_id=tenant, item_id="i", work_item_id="w", run_id="r"
        )
    )
    assert e_out.status == "sent"
    assert calls["draft_id"] == "draft-dual"  # the executor lane armed the send through the facade


def test_dual_role_with_gated_capability_is_accepted():
    """Contrast the pure-proposer rejection: a {PROPOSER, EXECUTOR} module MAY declare a gated
    capability — the manifest validates because EXECUTOR is a declared role."""
    _DualRoleModule.manifest.validate()  # does not raise
    AgentFrameworkRegistry().register(_DualRoleModule())  # registers cleanly


def test_dual_role_proposer_lane_cannot_send():
    """Even though the module DECLARES REQUEST_CUSTOMER_SEND, its PROPOSER-lane facade STRIPS gated
    capabilities — the proposer lane is structurally side-effect-free (holds only the read cap)."""
    registered = AgentFrameworkRegistry().register(_DualRoleModule())
    p_ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()), module_name="dual_role_module"
    )
    gate = registered.new_gate(p_ctx)
    assert gate.can(Capability.REQUEST_CUSTOMER_SEND) is False
    assert gate.capabilities == frozenset({Capability.READ_BUSINESS_CONTEXT})
    with pytest.raises(CapabilityNotDeclared):
        gate.request_customer_send("draft-1")


def test_dual_role_executor_lane_can_send():
    """The EXECUTOR lane of the SAME module DOES service the gated capability."""
    registered = AgentFrameworkRegistry().register(_DualRoleModule())
    e_ctx = ModuleContext.for_executor(
        tenant_id=uuid4(), item_id="i", work_item_id="w", run_id="r"
    )
    gate = registered.new_gate(e_ctx)
    assert gate.can(Capability.REQUEST_CUSTOMER_SEND) is True


def test_run_rejects_role_not_declared():
    """``run`` with a ``ctx.role`` the module does not declare raises ``ModuleDispatchError``."""
    from orchestrator.agent_framework import ModuleDispatchError

    registered = AgentFrameworkRegistry().register(
        BusinessContextReader(reader=lambda _t: _fake_business_context())
    )  # pure PROPOSER
    e_ctx = ModuleContext.for_executor(
        tenant_id=uuid4(), item_id="i", work_item_id="w", run_id="r"
    )
    with pytest.raises(ModuleDispatchError):
        registered.run(e_ctx)


def test_pure_proposer_with_gated_capability_still_rejected():
    """Regression guard for Ruling #1: a PURE proposer declaring a gated capability is REJECTED
    (only a module that ALSO declares EXECUTOR may)."""
    manifest = AgentManifest(
        name="pure_proposer_gated",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="pure proposer illegally declaring a gated capability",
        capabilities=frozenset({Capability.REQUEST_CUSTOMER_SEND}),
    )
    with pytest.raises(ManifestError):
        manifest.validate()


# --- 8. D-REG: manifest -> activation_registry (single source, EXPLICIT wiring) -----------------


def test_import_does_not_mutate_activation_registry():
    """Importing the framework wires NOTHING: the framework's own default registry is empty AND the
    activation registry carries no framework-injected probe entry (additive/inert)."""
    from orchestrator.agent_framework import default_registry
    from orchestrator.agents import activation_registry

    assert default_registry().names() == []
    assert "conformance_probe_agent" not in activation_registry.REGISTRY


def test_register_activation_prereqs_wires_and_is_idempotent():
    """The EXPLICIT wiring step publishes a manifest's declared bar into activation_registry, and a
    second call is a safe no-op."""
    from orchestrator.agent_framework import register_activation_prereqs
    from orchestrator.agents import activation_registry
    from orchestrator.agents.activation_registry import AgentPrerequisites

    class _Executor:
        manifest = AgentManifest(
            name="conformance_probe_agent",
            version="1.0.0",
            roles=frozenset({AgentRole.EXECUTOR}),
            description="probe module with a declared activation bar",
            capabilities=frozenset(),
            prerequisites=AgentPrerequisites(
                agent="conformance_probe_agent",
                requires_journey_complete=True,
                requires_verification=False,
                requires_enabled_data_source=False,
                min_customers=0,
                requires_ownership_verified=False,
            ),
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    assert "conformance_probe_agent" not in activation_registry.REGISTRY
    try:
        register_activation_prereqs(_Executor())
        assert "conformance_probe_agent" in activation_registry.REGISTRY
        got = activation_registry.get_prerequisites("conformance_probe_agent")
        assert got.requires_journey_complete is True
        assert got.requires_verification is False
        register_activation_prereqs(_Executor())  # idempotent — no raise
        assert list(activation_registry.REGISTRY).count("conformance_probe_agent") == 1
    finally:
        activation_registry.REGISTRY.pop("conformance_probe_agent", None)


def test_register_activation_prereqs_noop_without_bar():
    """A module with NO prerequisites (a read-only advisory lane) wires nothing."""
    from orchestrator.agent_framework import register_activation_prereqs
    from orchestrator.agents import activation_registry

    before = dict(activation_registry.REGISTRY)
    register_activation_prereqs(BusinessContextReader())  # prerequisites=None
    assert activation_registry.REGISTRY == before


# --- 9. D-ENT: SOFT, COMPUTED entitlement seam --------------------------------------------------


def test_entitlement_no_key_is_free():
    """A manifest with no ``entitlement_key`` is a FREE capability — always entitled."""
    from orchestrator.agent_framework import check_entitlement

    manifest = AgentManifest(
        name="free_agent",
        version="1.0.0",
        roles=frozenset({AgentRole.EXECUTOR}),
        description="free capability, no SKU",
    )
    assert check_entitlement(manifest, uuid4()) is True


def test_entitlement_with_key_is_soft_open():
    """A billable module (SKU declared) is SOFT-OPEN pre-launch — the seam NEVER hard-blocks and
    NEVER encodes a price; it documents where billing wires in at activation."""
    from orchestrator.agent_framework import check_entitlement

    manifest = AgentManifest(
        name="billable_agent",
        version="1.0.0",
        roles=frozenset({AgentRole.EXECUTOR}),
        description="billable capability with a SKU declaration",
        entitlement_key="sku_specialised_agent",
    )
    assert check_entitlement(manifest, uuid4()) is True
