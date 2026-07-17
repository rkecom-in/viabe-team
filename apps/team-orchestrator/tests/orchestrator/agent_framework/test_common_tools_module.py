"""Unit tests for the COMMON READ-tools agent_framework MODULE (``common_tools_module.py``).

Proves the module CONFORMS to the framework contract (``assert_conforms`` — all 9 checks, esp.
``tool_surface_safe`` over the three common read tools) and that its PROPOSER lane is a thin,
side-effect-free read that reports the common read-tool surface. Also pins the additive-shape
invariants: the manifest carries the EXACT ``COMMON_READ_TOOLS`` objects, declares the three
NON-GATED read capabilities and NO gated capability, and the proposer facade is structurally
read-only.

Dep discipline (mirrors the sibling ``test_integration_tools_module.py``): building the manifest
lazy-imports ``COMMON_READ_TOOLS`` (langchain), and ``assert_conforms``/``register`` pull the
deny-list guard (langchain via ``orchestrator.agent.__init__``). We ``importorskip('langchain')`` so
the dep-less smoke skips the whole module; the full suite runs all of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langchain")

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
from orchestrator.agent_framework.modules.common_tools_module import (  # noqa: E402
    MODULE_NAME,
    CommonToolsModule,
)
from orchestrator.agent_framework.tools_common import COMMON_READ_TOOLS  # noqa: E402

_EXPECTED_CAPS = frozenset(
    {
        Capability.READ_BUSINESS_CONTEXT,
        Capability.READ_CUSTOMER_LEDGER,
        Capability.READ_INTEGRATION_STATE,
    }
)

_EXPECTED_TOOL_NAMES = [
    "read_customer_ledger_summary",
    "read_business_context",
    "read_integration_state",
    "read_active_plan",  # VT-673
    "read_agent_memory",  # VT-674
]


# --- 1. conformance (the required gate) --------------------------------------------------------


def test_module_conforms():
    """``assert_conforms`` passes — every trust-boundary property the framework depends on holds,
    including ``tool_surface_safe`` over the three common read tools."""
    report = assert_conforms(CommonToolsModule())
    assert report.passed, str(report)
    assert {r.name for r in report.results} == set(CHECK_NAMES)
    assert all(r.passed for r in report.results), str(report)


def test_conformance_report_names_stable():
    report = check_module_conformance(CommonToolsModule())
    assert [r.name for r in report.results] == list(CHECK_NAMES)
    assert len(CHECK_NAMES) == 9  # VT-669 added required_tools_reachable


def test_tool_surface_safe_check_passes_explicitly():
    """The load-bearing check for THIS module: the three-tool read surface passes the deny-list."""
    report = check_module_conformance(CommonToolsModule())
    assert report.result("tool_surface_safe").passed, str(report)


# --- 2. manifest shape (pure PROPOSER, NON-GATED, three read tools) -----------------------------


def test_manifest_shape():
    m = CommonToolsModule().manifest
    assert m.name == MODULE_NAME == "common_tools"
    assert m.version == "1.0.0"
    assert m.roles == frozenset({AgentRole.PROPOSER})
    assert m.capabilities == _EXPECTED_CAPS
    # NO gated capability — pure PROPOSER; nothing on the read surface sends or spends.
    assert m.gated_capabilities == frozenset()
    assert Capability.REQUEST_CUSTOMER_SEND not in m.capabilities
    assert Capability.REQUEST_BUSINESS_ACTION not in m.capabilities
    assert m.prerequisites is None
    assert m.entitlement_key is None


def test_manifest_carries_the_three_common_read_tools():
    """``manifest.tools`` IS the exact ``COMMON_READ_TOOLS`` surface (same objects, in order)."""
    tools = CommonToolsModule().manifest.tools
    assert isinstance(tools, tuple)
    assert len(tools) == 5  # VT-673 read_active_plan + VT-674 read_agent_memory
    assert [t.name for t in tools] == _EXPECTED_TOOL_NAMES
    assert list(tools) == list(COMMON_READ_TOOLS)
    for t in tools:
        assert any(t is orig for orig in COMMON_READ_TOOLS)


# --- 3. PROPOSER lane: thin read entry (reports the tool surface) -------------------------------


def test_propose_reports_the_read_tool_surface():
    """``propose`` reports the common read-tool names as a proposal (no DB, no side effect).
    Round-trips onto the existing AgentResult envelope."""
    module = CommonToolsModule()
    registered = AgentFrameworkRegistry().register(module)
    ctx = ModuleContext.for_proposer(tenant_model_value=str(uuid4()), module_name=MODULE_NAME)

    result = registered.run(ctx)  # dispatches to propose() by ctx.role

    assert isinstance(result, ModuleResult)
    assert result.role is AgentRole.PROPOSER
    assert result.status == "completed"
    assert result.proposal == {"common_read_tools": _EXPECTED_TOOL_NAMES}

    agent_result = result.to_agent_result()
    assert agent_result.status == "completed"
    assert agent_result.output["common_read_tools"] == _EXPECTED_TOOL_NAMES


def test_tools_provider_is_injectable():
    """The tools surface is injectable (transport-injection convention): a fake provider drives both
    the manifest tool surface and what ``propose`` reports — no real langchain tools needed."""
    fake_tools = (SimpleNamespace(name="read_x"), SimpleNamespace(name="read_y"))
    module = CommonToolsModule(tools_provider=lambda: fake_tools)
    assert [t.name for t in module.manifest.tools] == ["read_x", "read_y"]

    ctx = ModuleContext.for_proposer(tenant_model_value=str(uuid4()), module_name=MODULE_NAME)
    result = module.propose(ctx, AgentFrameworkRegistry().register(module).new_gate(ctx))
    assert result.proposal == {"common_read_tools": ["read_x", "read_y"]}


# --- 4. structural read-only guarantee ----------------------------------------------------------


def test_proposer_lane_is_structurally_readonly():
    """The proposer-lane facade services ONLY the three non-gated read caps; a send/spend attempt
    raises ``CapabilityNotDeclared`` — the module is structurally unable to send or spend."""
    module = CommonToolsModule()
    registered = AgentFrameworkRegistry().register(module)
    ctx = ModuleContext.for_proposer(tenant_model_value=str(uuid4()), module_name=MODULE_NAME)
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
    reg = AgentFrameworkRegistry()
    reg.register(CommonToolsModule())
    assert reg.names() == ["common_tools"]
    assert "common_tools" in reg
