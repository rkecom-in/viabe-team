"""VT-669 — unit tests for the Onboarding-Conductor connector-tools agent_framework MODULE.

Proves the module CONFORMS to the framework contract (``assert_conforms`` — all 9 checks, esp.
``tool_surface_safe`` over the ten conductor tools + the new ``required_tools_reachable``) and that
its PROPOSER lane is a thin, side-effect-free read that reports the onboarding state + the conductor
tool surface WITHOUT touching a DB (the state reader is injected). Pins the additive-shape invariants:
the manifest carries the EXACT ``ONBOARDING_CONDUCTOR_TOOLS`` objects, declares NO gated capability,
and records the reads its job REQUIRES (VT-669 sufficiency, own-surface reachability).

Dep discipline (mirrors the sibling integration module test): ``assert_conforms``/``register`` reach
the deny-list guard (langchain), and building the manifest lazy-imports the conductor tools. We
``importorskip("langchain")`` so the dep-less smoke skips the whole module; the full suite runs it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("langchain")

from orchestrator.agent.onboarding_conductor import (  # noqa: E402
    ONBOARDING_CONDUCTOR_TOOLS,
)
from orchestrator.agent_framework import (  # noqa: E402
    CHECK_NAMES,
    AgentFrameworkRegistry,
    AgentRole,
    Capability,
    ModuleContext,
    ModuleResult,
    assert_conforms,
    check_module_conformance,
)
from orchestrator.agent_framework.modules.onboarding_conductor_module import (  # noqa: E402
    MODULE_NAME,
    REQUIRED_TOOLS,
    OnboardingConductorModule,
)

_EXPECTED_CAPS = frozenset(
    {Capability.READ_BUSINESS_CONTEXT, Capability.PROPOSE_CONFIG_CHANGE}
)

_EXPECTED_TOOL_NAMES = [
    "read_onboarding_state",
    "extract_owner_answer",
    "record_answer",
    "record_skip",
    "apply_correction",
    "next_required_question",
    "profile_completion_check",
    "activation_check",
    "propose_business_policy",
    "conductor_escalate_to_fazal",
]


# --- 1. conformance (the required gate, now 9 checks) ------------------------------------------


def test_module_conforms():
    """``assert_conforms`` passes — every trust-boundary + sufficiency property holds, including
    ``tool_surface_safe`` over the ten conductor tools and ``required_tools_reachable``."""
    report = assert_conforms(OnboardingConductorModule())
    assert report.passed, str(report)
    assert {r.name for r in report.results} == set(CHECK_NAMES)
    assert all(r.passed for r in report.results), str(report)


def test_required_tools_reachable_via_own_surface():
    """The conductor's required reads are on its OWN tool surface — the own-surface reachability path
    (the contrast to SR, whose required reads are Manager-scoped common reads)."""
    report = check_module_conformance(OnboardingConductorModule())
    assert report.result("required_tools_reachable").passed, str(report)
    for name in REQUIRED_TOOLS:
        assert name in _EXPECTED_TOOL_NAMES  # every required tool is one it actually holds


# --- 2. manifest shape (pure PROPOSER, NON-GATED, ten conductor tools + required_tools) ---------


def test_manifest_shape():
    m = OnboardingConductorModule().manifest
    assert m.name == MODULE_NAME == "onboarding_tools"
    assert m.version == "1.0.0"
    assert m.roles == frozenset({AgentRole.PROPOSER})
    assert m.capabilities == _EXPECTED_CAPS
    assert m.gated_capabilities == frozenset()
    assert Capability.REQUEST_CUSTOMER_SEND not in m.capabilities
    assert Capability.REQUEST_BUSINESS_ACTION not in m.capabilities
    assert m.prerequisites is None
    assert m.required_tools == REQUIRED_TOOLS == (
        "read_onboarding_state",
        "profile_completion_check",
        "activation_check",
    )


def test_manifest_carries_the_ten_conductor_tools():
    """``manifest.tools`` IS the exact ``ONBOARDING_CONDUCTOR_TOOLS`` surface (same objects, order)."""
    tools = OnboardingConductorModule().manifest.tools
    assert isinstance(tools, tuple)
    assert len(tools) == 10
    assert [t.name for t in tools] == _EXPECTED_TOOL_NAMES
    assert list(tools) == list(ONBOARDING_CONDUCTOR_TOOLS)


# --- 3. PROPOSER lane: thin read entry (state snapshot + tool surface) --------------------------


def test_propose_reports_state_and_tools():
    """``propose`` best-effort reads the onboarding state (injected) and reports it + the conductor
    tool names as a proposal."""
    captured = {}
    fake_state = {
        "status": "active",
        "answers": {"business_type": "cafe"},
        "skipped": [],
        "flow": None,
        "populated": {},
    }

    def fake_reader(tenant_id):
        captured["tenant_id"] = tenant_id
        return fake_state

    module = OnboardingConductorModule(state_reader=fake_reader)
    registered = AgentFrameworkRegistry().register(module)
    tenant = str(uuid4())
    ctx = ModuleContext.for_proposer(tenant_model_value=tenant, module_name=MODULE_NAME)

    result = registered.run(ctx)

    assert isinstance(result, ModuleResult)
    assert result.role is AgentRole.PROPOSER
    assert result.status == "completed"
    assert result.proposal["onboarding_state"] == fake_state
    assert result.proposal["conductor_tools"] == _EXPECTED_TOOL_NAMES
    assert captured["tenant_id"] == tenant


def test_propose_read_miss_reports_tools_only():
    """A read miss yields a ``None`` state (enrichment, not a failure) and still reports the tools."""

    def boom(tenant_id):
        raise RuntimeError("no journey row")

    module = OnboardingConductorModule(state_reader=boom)
    ctx = ModuleContext.for_proposer(tenant_model_value=str(uuid4()), module_name=MODULE_NAME)
    result = module.propose(ctx, AgentFrameworkRegistry().register(module).new_gate(ctx))

    assert result.status == "completed"
    assert result.proposal["onboarding_state"] is None
    assert result.proposal["conductor_tools"] == _EXPECTED_TOOL_NAMES
    assert result.reason == "onboarding_state_read_miss"
