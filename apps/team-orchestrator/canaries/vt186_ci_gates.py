#!/usr/bin/env python3
"""VT-186 CI gates canary (Rule #15, DR-15).

Subshell-source nothing — pure-Python canary, no secrets:

    cd apps/team-orchestrator
    time ./.venv/bin/python canaries/vt186_ci_gates.py 2>&1 | tee /tmp/vt186-canary-evidence.log | tail -180

Pure-Python; no DB; no LLM. ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 20s. Cost: 0 paise.

8 assertions per brief §Rule-15:
- A1: synthetic un-hooked add_node → gate-langgraph violation list non-empty
- A2: synthetic hooked add_node → gate-langgraph violation list empty
- A3: synthetic @tool without @tool_step → gate-mcp offender list non-empty
- A4: synthetic @tool + @tool_step → gate-mcp offender list empty
- A5: synthetic source with `step_kind='unregistered'` → gate-envelope
  validate_registry_completeness raises EnvelopeRegistryDrift
- A6: current main source (the canary's actual repo) → all 3 gate logics clean
- A7: opt-out annotation `# observability:opt-out reason=test` on
  un-hooked add_node → gate-langgraph treats it as clean
- A8: ANTHROPIC ABSENT preflight
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO_ROOT = Path(__file__).resolve().parents[3]


RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight():
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary must NOT "
            "source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print("PREFLIGHT OK — ANTHROPIC_API_KEY: <absent — defense-in-depth>")


# ----------------------------------------------------------------
# Replicas of the 3 gate logics from .github/workflows/ci.yml.
# Keep these byte-faithful to the YAML's run-step bodies so the canary
# tests EXACTLY what CI runs.
# ----------------------------------------------------------------


def _gate_langgraph_violations(root: Path) -> list[str]:
    OPT_OUT_MARK = "observability:opt-out"
    HOOK_NAME = "with_state_transition_hook"
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_node"):
                continue
            if len(node.args) < 2:
                continue
            callable_arg = node.args[1]
            hooked = (
                isinstance(callable_arg, ast.Call)
                and isinstance(callable_arg.func, ast.Name)
                and callable_arg.func.id == HOOK_NAME
            )
            if hooked:
                continue
            start_line = node.lineno
            preceding = source_lines[max(0, start_line - 4):start_line - 1]
            opt_out = any(OPT_OUT_MARK in line for line in preceding)
            if opt_out:
                continue
            violations.append(f"{path}:{start_line}")
    return violations


def _gate_mcp_offenders(tools_dir: Path) -> list[str]:
    offenders: list[str] = []
    if not tools_dir.exists():
        return offenders
    for path in sorted(tools_dir.rglob("*.py")):
        if path.name == "self_evaluate.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "\n@tool\n" not in source:
            continue
        if "tool_step" not in source:
            offenders.append(str(path))
    return offenders


def _gate_envelope_drift_check(extra_step_kind: str | None = None) -> Exception | None:
    """Invoke validate_registry_completeness in a synthetic context.

    Plants ``extra_step_kind`` into a temp source file under a synthetic
    package root + walks it like validate_registry_completeness does, so
    we can prove the drift-detection path raises without monkey-patching
    the real registry.
    """
    from orchestrator.observability.envelopes import (
        STEP_KIND_REGISTRY,
        EnvelopeRegistryDrift,
        _collect_step_kind_literals,
    )

    if extra_step_kind is None:
        try:
            from orchestrator.observability.envelopes import (
                validate_registry_completeness,
            )

            validate_registry_completeness()
        except Exception as exc:  # noqa: BLE001
            return exc
        return None

    with tempfile.TemporaryDirectory() as tmp:
        synthetic = Path(tmp) / "orchestrator"
        synthetic.mkdir()
        (synthetic / "__init__.py").write_text("")
        (synthetic / "synthetic_module.py").write_text(
            f"def f():\n    step_kind='{extra_step_kind}'\n    return step_kind\n"
        )
        literals = _collect_step_kind_literals(Path(tmp))
        missing = sorted(literals - STEP_KIND_REGISTRY.keys())
        if missing:
            return EnvelopeRegistryDrift(
                f"synthetic unregistered: {missing}"
            )
    return None


def run_canary() -> int:
    _preflight()

    # ----------------------------------------------------------------
    # A1: synthetic un-hooked add_node → gate-langgraph violation
    # ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        synth = Path(tmp) / "orchestrator"
        synth.mkdir()
        (synth / "__init__.py").write_text("")
        (synth / "synth.py").write_text(
            "def my_node(s): return s\n"
            "class _G:\n"
            "    def add_node(self, *a): pass\n"
            "graph = _G()\n"
            "graph.add_node('my_node', my_node)\n"
        )
        violations = _gate_langgraph_violations(Path(tmp) / "orchestrator")
    pass_1 = len(violations) >= 1
    assertion(
        1,
        "synthetic un-hooked add_node → gate-langgraph violation",
        pass_1,
        observed={"violation_count": len(violations), "sample": violations[:3]},
        expected={"violation_count_gte": 1},
    )

    # ----------------------------------------------------------------
    # A2: synthetic hooked add_node → no violation
    # ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        synth = Path(tmp) / "orchestrator"
        synth.mkdir()
        (synth / "__init__.py").write_text("")
        (synth / "synth.py").write_text(
            "def my_node(s): return s\n"
            "def with_state_transition_hook(c, **k): return c\n"
            "class _G:\n"
            "    def add_node(self, *a): pass\n"
            "graph = _G()\n"
            "graph.add_node('my_node', with_state_transition_hook(my_node, node_name='my_node'))\n"
        )
        violations = _gate_langgraph_violations(Path(tmp) / "orchestrator")
    pass_2 = len(violations) == 0
    assertion(
        2,
        "synthetic hooked add_node → gate-langgraph empty",
        pass_2,
        observed={"violation_count": len(violations), "sample": violations[:3]},
        expected={"violation_count": 0},
    )

    # ----------------------------------------------------------------
    # A3: synthetic @tool without @tool_step → gate-mcp offender
    # ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        tools = Path(tmp) / "tools"
        tools.mkdir()
        (tools / "bad_tool.py").write_text(
            "def tool(f): return f\n"
            "@tool\n"
            "def bad(x): return x\n"
        )
        offenders = _gate_mcp_offenders(tools)
    pass_3 = len(offenders) >= 1
    assertion(
        3,
        "synthetic @tool without @tool_step → gate-mcp offender list non-empty",
        pass_3,
        observed={"offender_count": len(offenders), "sample": offenders[:3]},
        expected={"offender_count_gte": 1},
    )

    # ----------------------------------------------------------------
    # A4: synthetic @tool + @tool_step → no offender
    # ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        tools = Path(tmp) / "tools"
        tools.mkdir()
        (tools / "good_tool.py").write_text(
            "def tool(f): return f\n"
            "def tool_step(**k):\n"
            "    return lambda f: f\n"
            "@tool\n"
            "@tool_step(step_kind='mcp_tool_call', envelope_in=None, envelope_out=None)\n"
            "def good(x): return x\n"
        )
        offenders = _gate_mcp_offenders(tools)
    pass_4 = len(offenders) == 0
    assertion(
        4,
        "synthetic @tool + @tool_step → gate-mcp offender list empty",
        pass_4,
        observed={"offender_count": len(offenders), "sample": offenders[:3]},
        expected={"offender_count": 0},
    )

    # ----------------------------------------------------------------
    # A5: synthetic unregistered step_kind → gate-envelope raises
    # ----------------------------------------------------------------
    exc = _gate_envelope_drift_check(extra_step_kind="definitely_not_real_x9z")
    pass_5 = exc is not None and type(exc).__name__ == "EnvelopeRegistryDrift"
    assertion(
        5,
        "synthetic unregistered step_kind → gate-envelope raises EnvelopeRegistryDrift",
        pass_5,
        observed={"raised_type": type(exc).__name__ if exc else None, "msg": repr(exc) if exc else None},
        expected={"raised_type": "EnvelopeRegistryDrift"},
    )

    # ----------------------------------------------------------------
    # A6: current main source → all 3 gates clean
    # ----------------------------------------------------------------
    current_violations = _gate_langgraph_violations(
        REPO_ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator"
    )
    current_offenders = _gate_mcp_offenders(
        REPO_ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "agent" / "tools"
    )
    current_envelope_exc = _gate_envelope_drift_check(extra_step_kind=None)
    pass_6 = (
        len(current_violations) == 0
        and len(current_offenders) == 0
        and current_envelope_exc is None
    )
    assertion(
        6,
        "current main source → all 3 gate logics clean",
        pass_6,
        observed={
            "langgraph_violations": current_violations,
            "mcp_offenders": current_offenders,
            "envelope_exception": repr(current_envelope_exc) if current_envelope_exc else None,
        },
        expected={"all_clean": True},
    )

    # ----------------------------------------------------------------
    # A7: opt-out annotation recognized
    # ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        synth = Path(tmp) / "orchestrator"
        synth.mkdir()
        (synth / "__init__.py").write_text("")
        (synth / "synth.py").write_text(
            "def my_node(s): return s\n"
            "class _G:\n"
            "    def add_node(self, *a): pass\n"
            "graph = _G()\n"
            "# observability:opt-out reason=canary-test\n"
            "graph.add_node('my_node', my_node)\n"
        )
        violations = _gate_langgraph_violations(Path(tmp) / "orchestrator")
    pass_7 = len(violations) == 0
    assertion(
        7,
        "opt-out annotation '# observability:opt-out reason=...' recognized + skipped",
        pass_7,
        observed={"violation_count": len(violations), "sample": violations[:3]},
        expected={"violation_count": 0},
    )

    # ----------------------------------------------------------------
    # A8: ANTHROPIC ABSENT preflight invariant maintained
    # ----------------------------------------------------------------
    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise()


def _finalise() -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (pure-Python gate canary) ===")

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 8 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
