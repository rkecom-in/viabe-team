"""VT-664 Stage 1 — unit tests for the Integration connector-tools agent_framework MODULE.

Proves the module CONFORMS to the framework contract (``assert_conforms`` — all 9 checks, esp.
``tool_surface_safe`` over the eleven connector tools) and that its PROPOSER lane is a thin,
side-effect-free read that reports the integration state + the connector tool surface WITHOUT
touching a DB (the state reader is injected). Also pins the additive-shape invariants: the manifest
carries the EXACT eleven ``INTEGRATION_AGENT_TOOLS`` objects, declares NO gated capability, and the
proposer facade is structurally read-only.

Dep discipline (mirrors ``tests/agent/test_agent_framework.py`` + the SR module test):
``assert_conforms`` (``name_registerable``) + ``register`` reach the deny-list guard (langchain via
``orchestrator.agent.__init__``), and building the module's manifest lazy-imports the integration
agent (langchain). We ``importorskip("langchain")`` so the dep-less smoke skips the whole module; the
full suite (deps present) runs all of it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

# Building the module manifest lazy-imports INTEGRATION_AGENT_TOOLS (langchain), and
# ``assert_conforms``/``register`` pull the deny-list guard (langchain). Skip in the dep-less smoke.
pytest.importorskip("langchain")

from orchestrator.agent.integration_agent import (  # noqa: E402 — after the importorskip guard
    INTEGRATION_AGENT_TOOLS,
)
from orchestrator.agent_framework import (  # noqa: E402
    CHECK_NAMES,
    AgentFrameworkRegistry,
    AgentRole,
    Capability,
    CapabilityNotDeclared,
    ModuleContext,
    ModuleResult,
    assert_conforms,
    check_module_conformance,
)
from orchestrator.agent_framework.modules.integration_tools_module import (  # noqa: E402
    MODULE_NAME,
    IntegrationToolsModule,
)

# The NON-GATED capability set the module declares.
_EXPECTED_CAPS = frozenset(
    {Capability.READ_INTEGRATION_STATE, Capability.PROPOSE_CONFIG_CHANGE}
)

_EXPECTED_TOOL_NAMES = [
    "list_supported_connectors",
    "read_integration_state",
    "start_oauth",
    "check_oauth_status",
    "pull_sample",
    "propose_mapping",
    "confirm_mapping",
    "commit_ingestion",
    "schedule_recurring_pull",
    "verify_connector",
    "integration_escalate_to_fazal",
]


# --- 1. conformance (the required gate) --------------------------------------------------------


def test_module_conforms():
    """``assert_conforms`` passes — every trust-boundary property the framework depends on holds,
    including ``tool_surface_safe`` over the eleven connector tools."""
    report = assert_conforms(IntegrationToolsModule())
    assert report.passed, str(report)
    # Full-coverage: the report carries every named check, and each one passed.
    assert {r.name for r in report.results} == set(CHECK_NAMES)
    assert all(r.passed for r in report.results), str(report)


def test_conformance_report_names_stable():
    """The report shape is stable (all 9 named checks present), via the pure entrypoint."""
    report = check_module_conformance(IntegrationToolsModule())
    assert [r.name for r in report.results] == list(CHECK_NAMES)
    assert len(CHECK_NAMES) == 9  # VT-669 added required_tools_reachable


def test_tool_surface_safe_check_passes_explicitly():
    """The load-bearing check for THIS module: the eleven-tool surface passes the deny-list."""
    report = check_module_conformance(IntegrationToolsModule())
    assert report.result("tool_surface_safe").passed, str(report)


# --- 2. manifest shape (pure PROPOSER, NON-GATED, eleven connector tools) ------------------------


def test_manifest_shape():
    m = IntegrationToolsModule().manifest
    assert m.name == MODULE_NAME == "integration_tools"
    assert m.version == "1.0.0"
    assert m.roles == frozenset({AgentRole.PROPOSER})
    assert m.capabilities == _EXPECTED_CAPS
    # NO gated capability — pure PROPOSER; nothing on the connector surface sends or spends.
    assert m.gated_capabilities == frozenset()
    assert Capability.REQUEST_CUSTOMER_SEND not in m.capabilities
    assert Capability.REQUEST_BUSINESS_ACTION not in m.capabilities
    assert m.prerequisites is None
    assert m.entitlement_key is None


def test_manifest_carries_the_eleven_connector_tools():
    """``manifest.tools`` IS the exact ``INTEGRATION_AGENT_TOOLS`` surface (same objects, in order) —
    the additive 'connector Tools' shape (ARCHITECTURE.md §5)."""
    tools = IntegrationToolsModule().manifest.tools
    assert isinstance(tools, tuple)
    assert len(tools) == 11
    assert [t.name for t in tools] == _EXPECTED_TOOL_NAMES
    # the exact @tool objects, not copies — this module wraps the surface, it does not re-author it.
    assert list(tools) == list(INTEGRATION_AGENT_TOOLS)
    for t in tools:
        assert any(t is orig for orig in INTEGRATION_AGENT_TOOLS)


# --- 3. PROPOSER lane: thin read entry (state snapshot + tool surface) ---------------------------


def test_propose_reports_state_and_tools():
    """``propose`` best-effort reads the integration state (injected) and reports it + the connector
    tool names as a proposal. Round-trips onto the existing AgentResult envelope."""
    captured = {}
    fake_state = {
        "phase": "mapping",
        "current_connector_id": "google_sheet",
        "pending_owner_input": {"awaiting": "field_mapping_confirm"},
    }

    def fake_reader(tenant_id):
        captured["tenant_id"] = tenant_id
        return fake_state

    module = IntegrationToolsModule(state_reader=fake_reader)
    registered = AgentFrameworkRegistry().register(module)
    tenant = str(uuid4())
    ctx = ModuleContext.for_proposer(
        tenant_model_value=tenant, module_name=MODULE_NAME
    )

    result = registered.run(ctx)  # dispatches to propose() by ctx.role

    assert isinstance(result, ModuleResult)
    assert result.role is AgentRole.PROPOSER
    assert result.status == "completed"
    assert result.proposal["integration_state"] == fake_state
    assert result.proposal["connector_tools"] == _EXPECTED_TOOL_NAMES
    # the module read for its OWN resolved tenant (no ambient dispatch → the parsed uuid).
    assert captured["tenant_id"] == tenant

    # generalization proof: the proposal maps back onto the existing AgentResult envelope.
    agent_result = result.to_agent_result()
    assert agent_result.status == "completed"
    assert agent_result.output["connector_tools"] == _EXPECTED_TOOL_NAMES


def test_propose_handles_no_onboarding_yet():
    """A tenant with no onboarding started (reader returns None) → a None state, tools still listed."""
    module = IntegrationToolsModule(state_reader=lambda _t: None)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()), module_name=MODULE_NAME
    )
    result = module.propose(
        ctx, AgentFrameworkRegistry().register(module).new_gate(ctx)
    )
    assert result.status == "completed"
    assert result.proposal["integration_state"] is None
    assert result.proposal["connector_tools"] == _EXPECTED_TOOL_NAMES


def test_propose_read_miss_reports_tools_only():
    """A read miss (reader raises) is enrichment-loss, not a failure: None state + a diagnostic
    reason, and the connector tool surface is STILL reported."""
    def boom(_tenant_id):
        raise RuntimeError("db unavailable")

    module = IntegrationToolsModule(state_reader=boom)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()), module_name=MODULE_NAME
    )
    result = module.propose(
        ctx, AgentFrameworkRegistry().register(module).new_gate(ctx)
    )
    assert result.status == "completed"
    assert result.proposal["integration_state"] is None
    assert result.proposal["connector_tools"] == _EXPECTED_TOOL_NAMES
    assert result.reason == "integration_state_read_miss"


# --- 4. structural read-only guarantee ----------------------------------------------------------


def test_proposer_lane_is_structurally_readonly():
    """The proposer-lane facade services ONLY the two non-gated caps; a send attempt raises
    ``CapabilityNotDeclared`` — the module is structurally unable to send/spend."""
    module = IntegrationToolsModule()
    registered = AgentFrameworkRegistry().register(module)
    ctx = ModuleContext.for_proposer(
        tenant_model_value=str(uuid4()), module_name=MODULE_NAME
    )
    gate = registered.new_gate(ctx)
    assert gate.capabilities == _EXPECTED_CAPS
    assert gate.can(Capability.REQUEST_CUSTOMER_SEND) is False
    assert gate.can(Capability.REQUEST_BUSINESS_ACTION) is False
    with pytest.raises(CapabilityNotDeclared):
        gate.request_customer_send("draft-1")
    with pytest.raises(CapabilityNotDeclared):
        gate.gate_business_action("SPEND", 100)


# --- 5. registration smoke ----------------------------------------------------------------------


def test_registers_once_under_its_name():
    """The module registers cleanly into a fresh registry under its stable name."""
    reg = AgentFrameworkRegistry()
    reg.register(IntegrationToolsModule())
    assert reg.names() == ["integration_tools"]
    assert "integration_tools" in reg
