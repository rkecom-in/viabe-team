"""VT-685 — unit tests for the Compliance specialist SKELETON (Codex-onboarding target #1).

Proves the module CONFORMS to the framework contract (``assert_conforms`` — all 9 checks, esp.
``tool_surface_safe`` over the one example tool) and that its PROPOSER lane is a thin,
side-effect-free read that reports the GSTR-1/3B filing-readiness snapshot WITHOUT touching a real
DB (the reader is injected). Also pins the additive-shape invariants: pure PROPOSER, NO gated
capability, ADVISORY entitlement declared, and the standalone ``gstr_filing_readiness_snapshot``
tool's resolve-first (IDOR-guarded) contract.

Dep discipline (mirrors ``test_integration_tools_module.py`` / ``test_common_tools_module.py``):
building the manifest lazy-imports langchain (``_compliance_tools``), and ``assert_conforms``/
``register`` pull the deny-list guard (langchain via ``orchestrator.agent.__init__``). We
``importorskip('langchain')`` so the dep-less smoke skips the whole module; the full suite runs it.
"""

from __future__ import annotations

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
from orchestrator.agent_framework.modules.compliance_tools_module import (  # noqa: E402
    MODULE_NAME,
    ComplianceToolsModule,
    _readiness_notes,
    gstr_filing_readiness_snapshot,
)

_EXPECTED_CAPS = frozenset(
    {Capability.READ_BUSINESS_CONTEXT, Capability.READ_CUSTOMER_LEDGER}
)

_FAKE_SNAPSHOT = {
    "gstin_verified": True,
    "ledger_months_present": ["2026-05", "2026-06"],
    "sales_entries_90d": 12,
    "readiness_notes": ["2 month(s) of sales-ledger history available to prepare a return summary from."],
}


# --- 1. conformance (the required gate) --------------------------------------------------------


def test_module_conforms():
    """``assert_conforms`` passes — every trust-boundary property the framework depends on holds,
    including ``tool_surface_safe`` over the one example tool."""
    report = assert_conforms(ComplianceToolsModule())
    assert report.passed, str(report)
    assert {r.name for r in report.results} == set(CHECK_NAMES)
    assert all(r.passed for r in report.results), str(report)


def test_conformance_report_names_stable():
    report = check_module_conformance(ComplianceToolsModule())
    assert [r.name for r in report.results] == list(CHECK_NAMES)
    assert len(CHECK_NAMES) == 9  # VT-669 added required_tools_reachable


def test_tool_surface_safe_check_passes_explicitly():
    """The load-bearing check for THIS module: the one-tool surface passes the deny-list."""
    report = check_module_conformance(ComplianceToolsModule())
    assert report.result("tool_surface_safe").passed, str(report)


def test_required_tools_reachable_is_vacuous():
    """This skeleton declares no ``required_tools`` (see TODO extension point #4) — the check is
    n/a, not a false pass."""
    report = check_module_conformance(ComplianceToolsModule())
    result = report.result("required_tools_reachable")
    assert result.passed
    assert "n/a" in result.detail


# --- 2. manifest shape (pure PROPOSER, NON-GATED, one example tool, advisory entitlement) --------


def test_manifest_shape():
    m = ComplianceToolsModule().manifest
    assert m.name == MODULE_NAME == "compliance_tools"
    assert m.version == "1.0.0"
    assert m.roles == frozenset({AgentRole.PROPOSER})
    assert m.capabilities == _EXPECTED_CAPS
    # NO gated capability — Phase-1 is ADVISORY/PREPARE-ONLY; nothing files/sends/spends/mutates.
    assert m.gated_capabilities == frozenset()
    assert Capability.REQUEST_CUSTOMER_SEND not in m.capabilities
    assert Capability.REQUEST_BUSINESS_ACTION not in m.capabilities
    assert m.prerequisites is None
    assert m.required_tools == ()
    assert m.entitlement_key == "compliance_agent"


def test_manifest_carries_the_one_example_tool():
    tools = ComplianceToolsModule().manifest.tools
    assert isinstance(tools, tuple)
    assert len(tools) == 1
    assert tools[0].name == "gstr_filing_readiness_snapshot"


# --- 3. PROPOSER lane: thin read entry (the readiness snapshot) ---------------------------------


def test_propose_reports_the_readiness_snapshot():
    """``propose`` reports the injected reader's snapshot as a proposal (no DB, no side effect).
    Round-trips onto the existing AgentResult envelope."""
    captured = {}

    def fake_reader(tenant_id):
        captured["tenant_id"] = tenant_id
        return _FAKE_SNAPSHOT

    module = ComplianceToolsModule(reader=fake_reader)
    registered = AgentFrameworkRegistry().register(module)
    tenant = uuid4()
    ctx = ModuleContext.for_proposer(tenant_model_value=str(tenant), module_name=MODULE_NAME)

    result = registered.run(ctx)  # dispatches to propose() by ctx.role

    assert isinstance(result, ModuleResult)
    assert result.role is AgentRole.PROPOSER
    assert result.status == "completed"
    assert result.proposal == _FAKE_SNAPSHOT
    # the module read for its OWN resolved tenant (no ambient dispatch -> the parsed uuid).
    assert captured["tenant_id"] == tenant

    agent_result = result.to_agent_result()
    assert agent_result.status == "completed"
    assert agent_result.output == _FAKE_SNAPSHOT


def test_reader_is_injectable_transport_convention():
    """The reader hook is the repo's transport-injection convention — a fake reader drives
    ``propose`` with no real DB/knowledge reads."""
    module = ComplianceToolsModule(reader=lambda _tid: {"gstin_verified": False,
                                                          "ledger_months_present": [],
                                                          "sales_entries_90d": 0,
                                                          "readiness_notes": ["x"]})
    ctx = ModuleContext.for_proposer(tenant_model_value=str(uuid4()), module_name=MODULE_NAME)
    result = module.propose(ctx, AgentFrameworkRegistry().register(module).new_gate(ctx))
    assert result.proposal["gstin_verified"] is False
    assert result.proposal["ledger_months_present"] == []


# --- 4. structural read-only guarantee ----------------------------------------------------------


def test_proposer_lane_is_structurally_readonly():
    """The proposer-lane facade services ONLY the two non-gated read caps; a send/spend attempt
    raises ``CapabilityNotDeclared`` — the module is structurally unable to file/send/spend."""
    module = ComplianceToolsModule()
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


# --- 5. registration smoke -----------------------------------------------------------------------


def test_registers_once_under_its_name():
    reg = AgentFrameworkRegistry()
    reg.register(ComplianceToolsModule())
    assert reg.names() == ["compliance_tools"]
    assert "compliance_tools" in reg


# --- 6. the standalone tool function: resolve-first, IDOR-guarded, never-raises -------------------


def test_gstr_snapshot_resolves_model_value_with_no_ambient_context(monkeypatch):
    """No ambient dispatch context (a direct/unit-test call) -> falls back to parsing the
    model-supplied value as a UUID (mirrors ``resolve_lane_tenant``'s documented fallback)."""
    import orchestrator.agent_framework.modules.compliance_tools_module as mod

    captured = {}

    def fake_compute(tenant_id):
        captured["tenant_id"] = tenant_id
        return dict(_FAKE_SNAPSHOT)

    monkeypatch.setattr(mod, "_compute_gstr_readiness", fake_compute)
    tenant = uuid4()

    result = gstr_filing_readiness_snapshot(str(tenant))

    assert result == _FAKE_SNAPSHOT
    assert captured["tenant_id"] == tenant


def test_gstr_snapshot_unresolvable_tenant_returns_structured_error():
    """An unresolvable tenant (garbage, no ambient context) returns the structured
    ``lane_tenant_error`` dict — NEVER a raise (would orphan a real tool_use)."""
    result = gstr_filing_readiness_snapshot("not-a-uuid")
    assert result == {
        "status": "error",
        "error": "gstr_filing_readiness_snapshot: no resolvable tenant context",
    }


# --- 7. readiness-notes logic: pure, honest, no fabricated verdict --------------------------------


def test_readiness_notes_unverified_and_empty_ledger():
    notes = _readiness_notes(
        gstin_verified=False, ledger_months_present=[], sales_entries_90d=0
    )
    assert any("not verified" in n for n in notes)
    assert any("connect a sales data source" in n for n in notes)


def test_readiness_notes_verified_but_no_recent_sales():
    notes = _readiness_notes(
        gstin_verified=True, ledger_months_present=["2026-01"], sales_entries_90d=0
    )
    assert any("trailing 90 days" in n for n in notes)
    assert any("1 month(s)" in n for n in notes)


def test_readiness_notes_verified_and_ready():
    notes = _readiness_notes(
        gstin_verified=True, ledger_months_present=["2026-05", "2026-06"], sales_entries_90d=9
    )
    assert not any("not verified" in n for n in notes)
    assert not any("No sales recorded" in n for n in notes)
    assert any("2 month(s)" in n for n in notes)
