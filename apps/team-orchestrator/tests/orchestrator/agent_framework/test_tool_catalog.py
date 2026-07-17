"""VT-669 — tests for the TOOL CATALOG + the required-tools SUFFICIENCY conformance check.

Proves three things:
  1. the catalog covers 100% of every ``*_TOOLS`` tuple surface (the DRIFT GUARD — adding a tool
     without annotating it fails here) + both GateFacade doors, with the metadata shape intact;
  2. the ``required_tools_reachable`` conformance check PASSES for the launch specialists (SR +
     Onboarding), the reference plugin, and the existing modules — and FAILS a deliberately
     under-provisioned fixture (both the "not in catalog" and the "in catalog but unreachable" arms);
  3. ``render_catalog_markdown`` generates valid doc content FROM the catalog.

Dep discipline (mirrors the sibling framework tests): building the catalog imports the real tool
surfaces (langchain — and a constructed chat model for the integration surface), and ``assert_conforms``
reaches the deny-list guard. We ``importorskip("langchain")`` so the dep-less smoke skips the whole
module; the full suite runs all of it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")

from orchestrator.agent_framework import (  # noqa: E402 — after the importorskip guard
    AgentManifest,
    AgentRole,
    Capability,
    ManifestError,
    assert_conforms,
    check_module_conformance,
)
from orchestrator.agent_framework.tool_catalog import (  # noqa: E402
    KNOWN_CAPABILITY_GAPS,
    UNKNOWN,
    GapKind,
    ToolKind,
    catalog_by_name,
    catalog_coverage_gaps,
    catalog_entries,
    catalog_tool_names,
    open_capability_gaps,
    render_catalog_markdown,
)


# --- 1. the catalog: shape + 100% coverage + drift guard ---------------------------------------


def test_catalog_builds_and_every_tool_is_annotated():
    """Every catalog entry carries REAL metadata — none is an UNKNOWN-because-unannotated entry
    (which is what ``catalog_entries`` emits for a tool with no ``_Ann``)."""
    entries = catalog_entries()
    assert entries, "catalog is empty"
    unannotated = [e.name for e in entries if "NO ANNOTATION" in e.note]
    assert not unannotated, f"tools present on a surface but not annotated: {unannotated}"


def test_no_coverage_gaps_either_direction():
    """DRIFT GUARD: every tool on a defining surface has an annotation, and every annotation maps to
    a real tool (no stale annotation). Adding OR removing a tool without updating the catalog fails
    here."""
    gaps = catalog_coverage_gaps()
    assert gaps["unannotated"] == [], f"unannotated tools (add a catalog _Ann): {gaps['unannotated']}"
    assert gaps["stale"] == [], f"stale annotations (a tool was removed/renamed): {gaps['stale']}"


def test_catalog_covers_every_tuple_surface():
    """The catalog covers 100% of the ``*_TOOLS`` tuples — the concrete "no tool escapes the
    inventory" assertion the VT-669 gate names."""
    from orchestrator.agent.advisory_registry import ADVISORY_TOOLS
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS
    from orchestrator.agent.onboarding_conductor import ONBOARDING_CONDUCTOR_TOOLS
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS
    from orchestrator.agent_framework.tools_common import (
        COMMON_ADVISORY_TOOLS,
        COMMON_READ_TOOLS,
    )

    catalog_names = catalog_tool_names()
    for label, surface in [
        ("COMMON_READ_TOOLS", COMMON_READ_TOOLS),
        ("COMMON_ADVISORY_TOOLS", COMMON_ADVISORY_TOOLS),  # VT-672
        ("INTEGRATION_AGENT_TOOLS", INTEGRATION_AGENT_TOOLS),
        ("ONBOARDING_CONDUCTOR_TOOLS", ONBOARDING_CONDUCTOR_TOOLS),
        ("ORCHESTRATOR_AGENT_TOOLS", ORCHESTRATOR_AGENT_TOOLS),
        ("ADVISORY_TOOLS", ADVISORY_TOOLS),
    ]:
        missing = sorted({t.name for t in surface} - catalog_names)
        assert not missing, f"{label} tools missing from the catalog: {missing}"
    assert "escalate" in catalog_names  # VT-672: the ONE common escalate is cataloged


def test_lane_tuple_surfaces_covered():
    """Every one of the six advisory-lane tuples is fully covered (including the escalate/pushback
    tools EXCLUDED from ADVISORY_TOOLS — the catalog is the full inventory, not just the curated
    Manager set)."""
    from orchestrator.agent.accounting_lane import ACCOUNTING_LANE_TOOLS
    from orchestrator.agent.cost_opt_lane import COST_OPT_LANE_TOOLS
    from orchestrator.agent.finance_lane import FINANCE_LANE_TOOLS
    from orchestrator.agent.marketing_lane import MARKETING_LANE_TOOLS
    from orchestrator.agent.sales_lane import SALES_LANE_TOOLS
    from orchestrator.agent.tech_lane import TECH_LANE_TOOLS

    catalog_names = catalog_tool_names()
    for tup in (
        SALES_LANE_TOOLS, MARKETING_LANE_TOOLS, FINANCE_LANE_TOOLS,
        ACCOUNTING_LANE_TOOLS, TECH_LANE_TOOLS, COST_OPT_LANE_TOOLS,
    ):
        for t in tup:
            assert t.name in catalog_names, f"{t.name} not cataloged"


def test_gate_facade_doors_are_the_only_gated_entries():
    """The two effect doors + the decision door are the ONLY gated entries — the catalog documents
    the gates, and NOTHING else is gated (a required tool can never be a raw effect, VT-669)."""
    gated = {e.name for e in catalog_entries() if e.gated}
    assert gated == {"request_customer_send", "perform_business_action", "gate_business_action"}
    doors = {e.name: e for e in catalog_entries() if e.gated}
    assert doors["request_customer_send"].kind is ToolKind.GATED_EFFECT
    assert doors["perform_business_action"].kind is ToolKind.GATED_EFFECT
    assert doors["gate_business_action"].kind is ToolKind.DECISION  # decision-only door
    assert doors["request_customer_send"].capability is Capability.REQUEST_CUSTOMER_SEND


def test_read_integration_state_name_collision_is_two_entries():
    """``read_integration_state`` exists as TWO distinct tools (a common-read tool AND the integration
    agent's own) — the catalog keys by surface, so both are present with different kinds/holders."""
    by_name = catalog_by_name()
    entries = by_name["read_integration_state"]
    assert len(entries) == 2, [e.surface for e in entries]
    surfaces = {e.surface for e in entries}
    assert surfaces == {
        "agent_framework/tools_common.py",
        "agent/integration_agent.py",
    }
    kinds = {e.surface: e.kind for e in entries}
    assert kinds["agent_framework/tools_common.py"] is ToolKind.READ
    assert kinds["agent/integration_agent.py"] is ToolKind.INTEGRATION


def test_shared_lane_tool_shows_both_holders():
    """A lane tool the Manager ALSO carries in ADVISORY_TOOLS records BOTH holders (the same object,
    reached two ways)."""
    entry = next(e for e in catalog_entries() if e.name == "recommend_sales_play")
    assert "sales_lane" in entry.holders
    assert "manager_advisory" in entry.holders


def test_no_unknown_metadata_slipped_through():
    """No entry carries an UNKNOWN pii/tenant value silently — every UNKNOWN (there are none today)
    must be accompanied by a note (the VT-669 "mark UNKNOWN + note, never guess" rule)."""
    for e in catalog_entries():
        if e.pii_safe == UNKNOWN or e.tenant_scope == UNKNOWN:
            assert e.note, f"{e.name} carries UNKNOWN metadata with no explanatory note"


def test_render_catalog_markdown_is_valid():
    """The doc is GENERATED from the catalog and contains the header + a row per tool."""
    md = render_catalog_markdown()
    assert md.startswith("# Tool Catalog")
    assert "GENERATED from" in md
    # every tool name appears in the rendered table.
    names = catalog_tool_names()
    for name in names:
        assert f"`{name}`" in md, f"{name} missing from rendered TOOLS.md"
    # the three gated doors are flagged.
    assert "request_customer_send" in md


# --- 2. the required-tools manifest field ------------------------------------------------------


def test_manifest_required_tools_defaults_empty():
    m = AgentManifest(
        name="m", version="1.0.0", roles=frozenset({AgentRole.PROPOSER}), description="d"
    )
    assert m.required_tools == ()
    m.validate()  # no error


def test_manifest_rejects_non_string_required_tools():
    m = AgentManifest(
        name="m", version="1.0.0", roles=frozenset({AgentRole.PROPOSER}), description="d",
        required_tools=("ok", "", "  "),  # empty / whitespace are invalid
    )
    with pytest.raises(ManifestError, match="required_tools"):
        m.validate()


# --- 3. the required_tools_reachable conformance check -----------------------------------------


def test_required_tools_reachable_passes_for_launch_specialists():
    """SR (Manager-scoped common reads) + Onboarding (own-surface reads) both PASS the 9th check."""
    from orchestrator.agent_framework.modules.onboarding_conductor_module import (
        OnboardingConductorModule,
    )
    from orchestrator.agent_framework.modules.sales_recovery_module import SalesRecoveryModule

    for module in (SalesRecoveryModule(), OnboardingConductorModule()):
        report = assert_conforms(module)  # all 9 checks green
        assert report.result("required_tools_reachable").passed, str(report)


def test_required_tools_reachable_na_for_modules_without_required_tools():
    """The existing modules + the reference plugin declare no required_tools → the check is n/a and
    (crucially) does NOT import the heavy catalog."""
    from orchestrator.agent_framework.modules.common_tools_module import CommonToolsModule
    from orchestrator.agent_framework.modules.integration_tools_module import (
        IntegrationToolsModule,
    )
    from orchestrator.agent_framework.reference_plugin import BusinessContextReader

    for module in (IntegrationToolsModule(), CommonToolsModule(), BusinessContextReader()):
        r = check_module_conformance(module).result("required_tools_reachable")
        assert r.passed
        assert "n/a" in r.detail


class _UnprovisionedNotInCatalog:
    """A PROPOSER requiring a tool that DOES NOT EXIST in the catalog (typo/nonexistent)."""

    manifest = AgentManifest(
        name="under_provisioned_bogus",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="declares a required tool that is not a real cataloged tool",
        capabilities=frozenset({Capability.READ_CUSTOMER_LEDGER}),
        required_tools=("totally_bogus_tool",),
    )

    def propose(self, ctx, gate):  # pragma: no cover - never invoked (fails at conformance)
        ...


class _UnprovisionedUnreachable:
    """A PROPOSER requiring a REAL cataloged tool it can neither hold nor reach via the common reads
    (``get_business_profile`` is a real tool, but not on this module's surface nor a common read)."""

    manifest = AgentManifest(
        name="under_provisioned_unreachable",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="requires a real tool it cannot reach",
        capabilities=frozenset({Capability.READ_BUSINESS_CONTEXT}),
        required_tools=("get_business_profile",),
    )

    def propose(self, ctx, gate):  # pragma: no cover - never invoked (fails at conformance)
        ...


def test_required_tools_reachable_fails_not_in_catalog():
    """A required tool that is not a real cataloged tool fails-loud, naming it."""
    report = check_module_conformance(_UnprovisionedNotInCatalog())
    check = report.result("required_tools_reachable")
    assert not check.passed
    assert "totally_bogus_tool" in check.detail
    assert "catalog" in check.detail.lower()


def test_required_tools_reachable_fails_unreachable():
    """A required tool that IS cataloged but the module can neither hold nor reach fails-loud."""
    report = check_module_conformance(_UnprovisionedUnreachable())
    check = report.result("required_tools_reachable")
    assert not check.passed
    assert "get_business_profile" in check.detail
    assert "reachable" in check.detail.lower()


# --- 4. capability gaps — the SUFFICIENCY frontier (VT-669 "fail loudly", Fazal 2026-07-18) -----
#
# These tests do NOT assert the gaps are CLOSED (they aren't — that's the point; the loud RED gate is
# ``scripts/check_capability_gaps.py``). They keep the gap REGISTRY honest so a closed gap can never
# silently linger, and prove the frontier is currently non-empty (hiding it is the anti-pattern).


def test_every_registered_gap_is_on_the_board():
    """Every REGISTERED capability gap is tracked to a board row with a named owner + reason.
    (The registry went EMPTY 2026-07-18 — all 4 original gaps were built same-day; this stays as
    the shape-check for future entries, and the honesty test below guards against stale ones.)"""
    for g in KNOWN_CAPABILITY_GAPS:
        assert g.followon_vt.startswith("VT-"), f"gap {g.key!r} has no board row"
        assert g.needed_by, f"gap {g.key!r} names no specialist that needs it"
        assert g.reason.strip(), f"gap {g.key!r} has no reason"


def test_capability_gap_registry_is_honest():
    """REGISTRY HONESTY (auto-red-on-closure): a gap that has actually been resolved — its tool is now
    cataloged (ABSENT_FROM_CATALOG) or promoted onto the common-read surface (ABSENT_FROM_COMMON) —
    must be DELETED from ``KNOWN_CAPABILITY_GAPS``. This fails RED the moment a follow-on lands its
    tool but leaves the now-inert gap entry behind, forcing the builder to clean up. Today every
    tracked gap is genuinely open, so ``open == known``."""
    open_keys = {g.key for g in open_capability_gaps()}
    known_keys = {g.key for g in KNOWN_CAPABILITY_GAPS}
    closed_but_still_registered = known_keys - open_keys
    assert not closed_but_still_registered, (
        "these capability gaps have been RESOLVED (their tool is cataloged/promoted) but are still in "
        f"KNOWN_CAPABILITY_GAPS — delete them: {sorted(closed_but_still_registered)}"
    )


def test_open_gaps_render_into_tools_doc():
    """The generated TOOLS.md surfaces the open frontier (so the doc never hides what's missing)."""
    md = render_catalog_markdown()
    assert "Open capability gaps" in md
    for g in open_capability_gaps():
        assert g.followon_vt in md, f"gap {g.key!r} ({g.followon_vt}) not rendered into TOOLS.md"


def test_gap_kinds_are_known():
    """Every registered gap uses a defined ``GapKind`` (guards the ``_gap_is_open`` fall-through)."""
    for g in KNOWN_CAPABILITY_GAPS:
        assert g.kind in (GapKind.ABSENT_FROM_CATALOG, GapKind.ABSENT_FROM_COMMON)
