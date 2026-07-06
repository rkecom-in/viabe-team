"""VT-611 Phase B1 #6 — advisory-tool tests: no spawn/mutation/execution claims.

The promotion-gate ask, verbatim: "advisory-tool tests (no spawn/mutation/execution claims)."

Two halves (Cowork steer — BODY-level, not just the name-substring guard):

  1. ``assert_agent_tools_safe`` (VT-268, exercised structurally in #5's
     test_vt611_prompt_injection.py::test_no_specialist_or_lane_surface_holds_a_direct_effect_tool)
     is NAME-only — it checks the tool's exposed NAME against a forbidden-capability substring
     list, never what the function BODY actually does. A tool could be named innocuously
     ("analyze_tenant_spend") while its body still executed a raw SQL mutation or called a
     spawn/dispatch/write function directly. This file adds the missing BODY-level pin: AST/
     source-inspection of every advisory tool across the six VT-604 lanes for (a) a raw SQL
     INSERT/UPDATE/DELETE literal passed to a `.execute()`-shaped call, and (b) a call whose OWN
     name matches a mutation/send/dispatch/spawn/grant verb — reusing the house's own
     ``FORBIDDEN_CAPABILITY_SUBSTRINGS`` vocabulary (VT-268) rather than inventing a second,
     driftable taxonomy, plus a small advisory-specific extension for bare "insert"/"update"/
     "delete"/"dispatch_"/"spawn_"/"grant_" verbs the name-only guard doesn't need (a NAME like
     "spawn_cost_opt" is a legitimate ROSTER field elsewhere, not a tool; a CALL to something
     named ``spawn_x`` from INSIDE a tool body would be a real red flag).

  2. The StepKind/PlanStep validator forcing ``advisory_tool`` steps to declare NO effects
     (``allowed_effect_classes == []``) is ALREADY fully proven —
     ``test_plan_models.py::test_advisory_tool_step_cannot_declare_effects`` +
     ``test_advisory_tool_step_with_no_effects_constructs``. Referenced, not re-tested; this file
     adds ONE self-contained mirror (matching this row's #2 pattern for
     test_supervisor_loop_mode.py) so the VT-611 evidence manifest doesn't depend on grep-ing a
     VT-605 test file for its own gate.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

import pytest

pytest.importorskip("langchain")

from orchestrator.agent.tool_guardrail import FORBIDDEN_CAPABILITY_SUBSTRINGS  # noqa: E402

# Advisory-body-specific additions to the house's VT-268 name vocabulary: bare verb prefixes that
# would be a red flag as a CALL made *from inside* an advisory tool's body (a direct mutation/
# routing side effect), even though they're not useful as a tool NAME guard (e.g. "spawn_cost_opt"
# is a legitimate roster field elsewhere in the codebase, not a forbidden tool name).
_ADVISORY_BODY_MUTATION_VERBS: tuple[str, ...] = (
    "insert", "update", "delete", "dispatch_", "spawn_", "grant_", "start_workflow",
)
_ALL_MUTATION_VERBS = tuple(FORBIDDEN_CAPABILITY_SUBSTRINGS) + _ADVISORY_BODY_MUTATION_VERBS

_SQL_MUTATION_KEYWORDS = ("INSERT", "UPDATE", "DELETE")


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _find_mutation_hits(fn: Any) -> list[str]:
    """AST-walk one tool function's source for (a) a call whose own name matches a mutation/
    send/dispatch/spawn/grant verb, or (b) a raw SQL INSERT/UPDATE/DELETE literal passed to an
    execute-shaped call. Returns human-readable hit descriptions (empty = clean)."""
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        lname = name.lower()
        for verb in _ALL_MUTATION_VERBS:
            if verb in lname:
                hits.append(f"{fn.__name__}: call to {name!r} matches mutation verb {verb!r}")
                break
        if lname in ("execute", "executemany"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    head = arg.value.strip().upper()
                    for kw in _SQL_MUTATION_KEYWORDS:
                        if head.startswith(kw):
                            hits.append(f"{fn.__name__}: raw SQL {kw} literal passed to .execute()")
    return hits


def _lane_tools(module_path: str, attr: str) -> list[Any]:
    import importlib

    mod = importlib.import_module(module_path)
    return list(getattr(mod, attr))


# (module path, tool-list attr name) for all six VT-604 advisory lanes.
_LANE_TOOL_LISTS: list[tuple[str, str]] = [
    ("orchestrator.agent.accounting_lane", "ACCOUNTING_LANE_TOOLS"),
    ("orchestrator.agent.cost_opt_lane", "COST_OPT_LANE_TOOLS"),
    ("orchestrator.agent.finance_lane", "FINANCE_LANE_TOOLS"),
    ("orchestrator.agent.marketing_lane", "MARKETING_LANE_TOOLS"),
    ("orchestrator.agent.sales_lane", "SALES_LANE_TOOLS"),
    ("orchestrator.agent.tech_lane", "TECH_LANE_TOOLS"),
]


def _all_advisory_tool_functions() -> list[tuple[str, Any]]:
    """[(lane_name, plain_function)] for every tool across all six lanes — ``.func`` unwraps the
    langchain ``@tool`` decorator to the underlying callable (the VT-599 test's own convention)."""
    out: list[tuple[str, Any]] = []
    for module_path, attr in _LANE_TOOL_LISTS:
        lane_name = module_path.rsplit(".", 1)[-1]
        for tool in _lane_tools(module_path, attr):
            fn = getattr(tool, "func", tool)
            out.append((lane_name, fn))
    return out


@pytest.mark.parametrize(
    "entry", _all_advisory_tool_functions(),
    ids=lambda e: f"{e[0]}.{e[1].__name__}" if isinstance(e, tuple) else str(e),
)
def test_advisory_tool_body_has_no_mutation_or_routing_call(entry: tuple[str, Any]) -> None:
    """BODY-level pin (the recon's caveat on assert_agent_tools_safe being name-only): every tool
    across all six advisory lanes' OWN source is free of a raw SQL mutation literal and free of a
    call to anything named like a write/send/dispatch/spawn/grant/commit verb. Advisory tools may
    freely call READ helpers (get_tenant_cost, resolve_lane_tenant, logger.info, …) — none of
    which match this vocabulary, confirmed empirically before this test was written."""
    lane_name, fn = entry
    hits = _find_mutation_hits(fn)
    assert hits == [], f"{lane_name}: {hits}"


def test_at_least_one_tool_checked_per_lane() -> None:
    """Guard against the parametrize silently collecting zero tools for a lane (an import-path
    typo would otherwise pass vacuously)."""
    seen_lanes = {lane for lane, _ in _all_advisory_tool_functions()}
    assert seen_lanes == {
        "accounting_lane", "cost_opt_lane", "finance_lane",
        "marketing_lane", "sales_lane", "tech_lane",
    }


def test_checker_is_not_vacuous() -> None:
    """Self-test: the checker actually catches a synthetic violation of each of its two rules —
    guards against a future refactor of ``_find_mutation_hits`` silently making it a no-op."""

    def _evil_sql_write(conn: Any) -> None:
        conn.execute("INSERT INTO tenants (id) VALUES (%s)", ("x",))

    def _evil_direct_call() -> None:
        # Names below are never actually looked up — the checker only parses+walks this
        # function's SOURCE (inspect.getsource), it never executes the body.
        send_whatsapp_message: Any = None
        send_whatsapp_message("+910000000000", "hi")

    def _evil_spawn_call() -> None:
        spawn_integration: Any = None
        spawn_integration()

    assert _find_mutation_hits(_evil_sql_write) != []
    assert _find_mutation_hits(_evil_direct_call) != []
    assert _find_mutation_hits(_evil_spawn_call) != []


# --- StepKind validator: advisory_tool steps cannot declare effects (VT-605, already proven) ----


def test_advisory_tool_step_cannot_declare_effects_vt611_pin() -> None:
    """Self-contained VT-611 mirror of test_plan_models.py::
    test_advisory_tool_step_cannot_declare_effects (ALREADY landed at VT-605) — so the evidence
    manifest doesn't depend on grep-ing a different row's test file for this row's own gate."""
    from pydantic import ValidationError

    from orchestrator.manager.plan_models import PlanStep

    with pytest.raises(ValidationError):
        PlanStep(step_seq=1, kind="advisory_tool", allowed_effect_classes=["spend"])

    step = PlanStep(step_seq=1, kind="advisory_tool")
    assert step.allowed_effect_classes == []
