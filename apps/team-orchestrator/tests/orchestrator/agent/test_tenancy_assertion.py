"""VT-604 Package 1 — build-time assertion: every tenant-scoped agent tool uses
context-derived tenancy.

Extends the VT-599/603 invariant (``resolve_lane_tenant`` — the ambient dispatch
``ObservabilityContext`` is ALWAYS authoritative; a model-supplied ``tenant_id`` is
observed, never trusted) into a STATIC, CI-checkable gate over the Manager's FULL
runtime tool inventory: its own base tools, the three roster specialists' tool
modules, and the VT-604 advisory-tool registry. A future tool that declares a
``tenant_id`` parameter but forgets to resolve it from context (trusting the model's
value directly — the VT-293/294 IDOR class, and the live VT-598/599 defect) fails
this test immediately, at collection time, without needing a live run to surface it.

Mirrors the static-gating STYLE of ``scripts/check_no_direct_tenant_db_access.py``
(source-text scan + an explicit, reviewed scope) but as a plain pytest test — the
scope here is a Python object graph (bound tool lists), not a directory walk, so a
test fits better than a standalone script per the discipline of matching the check
to what it inspects.

Two context-derivation patterns are recognized as SAFE (both observed live in this
codebase, both context-first / model-value-observed-not-trusted):
  1. ``resolve_lane_tenant(...)`` — the shared VT-599 helper every lane tool calls.
  2. ``_observability_context.get()`` — the inline pattern the Manager's OWN tools
     use directly (``record_business_objective``), pre-dating the shared helper.

``compose_owner_output_tool`` (agent/tools/compose_output.py) is DELIBERATELY OUT OF
SCOPE: VT-590 removed it from the Manager's bound tool inventory (its output is
discarded — see orchestrator_agent.py), so it is not part of "the manager's runtime
tool inventory" this test binds. It uses a THIRD safe pattern of its own
(``tenant_from_context=True`` on ``@tool_step``) should it ever be re-added.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")

_SAFE_MARKERS = ("resolve_lane_tenant(", "_observability_context")


def _bound_tools() -> list[tuple[str, object]]:
    """Every tool in the Manager's actual runtime inventory: its own base tools +
    the three roster specialists' tool modules + the VT-604 advisory registry.

    Deliberately NOT ``roster_spawn_tools()`` — the spawn/handoff tools are built by
    the generic ``make_spawn_tool`` factory (Command(goto, graph=PARENT)); none
    declares a ``tenant_id`` parameter (they read run identity from graph state via
    ``InjectedState`` / the spec's ``update_builder``, a different, already-reviewed
    mechanism — see ``test_roster_registry.py``'s
    ``test_uuid_run_identity_still_required_for_existing_handoff``).
    """
    from orchestrator.agent.advisory_registry import ADVISORY_TOOLS
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS
    from orchestrator.agent.onboarding_conductor import ONBOARDING_CONDUCTOR_TOOLS
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS

    tools: list[object] = [
        *ORCHESTRATOR_AGENT_TOOLS,
        *INTEGRATION_AGENT_TOOLS,
        *ONBOARDING_CONDUCTOR_TOOLS,
        *ADVISORY_TOOLS,
    ]
    return [(getattr(t, "name", type(t).__name__), t) for t in tools]


def test_every_tenant_scoped_tool_uses_context_derived_tenancy() -> None:
    checked: list[str] = []
    for name, tool in _bound_tools():
        func = getattr(tool, "func", None)
        if func is None:
            continue
        params = inspect.signature(func).parameters
        if "tenant_id" not in params:
            continue
        source = inspect.getsource(func)
        assert any(marker in source for marker in _SAFE_MARKERS), (
            f"tool {name!r} declares a tenant_id parameter but its source shows no "
            f"context-derived tenancy resolution ({_SAFE_MARKERS!r}) — a "
            f"model-supplied tenant_id must NEVER be trusted directly (VT-599/603/604)."
        )
        checked.append(name)

    # Sanity floor: the known-affected tools (VT-599's ~23 + onboarding_conductor's 2 +
    # the manager's own record_business_objective) were actually inspected, not silently
    # skipped by an import/collection change. Not an exact pin — new tenant-scoped tools
    # are expected to grow this list; a COLLAPSE toward zero is the bug this guards.
    assert len(checked) >= 20, (
        f"expected at least 20 tenant-scoped tools to be checked, got {len(checked)}: "
        f"{sorted(checked)} — a collection/import change may have silently emptied scope"
    )


def test_a_tool_trusting_the_model_tenant_id_directly_fails_the_gate() -> None:
    """Negative control: a synthetic tool that declares ``tenant_id`` but never resolves
    it from context MUST fail the same assertion this module runs — proves the gate
    actually discriminates, not just passes vacuously on the real surface."""
    from langchain_core.tools import tool

    @tool
    def fake_untrusted_tool(tenant_id: str) -> dict[str, str]:
        """A tool that trusts the model-supplied tenant_id directly (the VT-599 defect)."""
        return {"tenant_id": tenant_id}

    func = fake_untrusted_tool.func  # type: ignore[attr-defined]
    source = inspect.getsource(func)
    assert not any(marker in source for marker in _SAFE_MARKERS)
