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
        role=AgentRole.PROPOSER,
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
        role=AgentRole.EXECUTOR,
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
            role=AgentRole.PROPOSER,
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
        role=AgentRole.PROPOSER,
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
            role=AgentRole.PROPOSER,
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
            role=AgentRole.EXECUTOR,
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
        role=AgentRole.EXECUTOR,
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
            role=AgentRole.EXECUTOR,
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
