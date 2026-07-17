"""Module conformance — THE "simple verification process" for ANY agent-framework module.

This is what makes the framework PURELY modular: a module (first-party OR a future third-party /
Codex-authored one) is verified by ONE call — ``assert_conforms(MyModule())`` in a test, or
``check_module_conformance(MyModule())`` for a structured report. You do NOT hand-audit a new
module against the contract; you run the conformance suite, which asserts every property the trust
boundary depends on.

TWO entrypoints:
  - ``check_module_conformance(module) -> ConformanceReport`` — PURE. Runs every check, catches
    everything, RAISES NOTHING. Returns a structured pass/fail per named check (introspectable /
    loggable / assertable). Use it to report, gate CI, or diff a module's compliance over time.
  - ``assert_conforms(module) -> ConformanceReport`` — the pytest helper. Runs the report and fails
    the test at the FIRST violation (with the check name + detail). Use it in a module's own test.

THE CHECKS (each a named result in the report):
  - ``has_manifest``                 — the module exposes an ``AgentManifest`` ``manifest`` attribute.
  - ``manifest_valid``               — ``manifest.validate()`` passes (structural + role/capability).
  - ``capabilities_legal_for_roles`` — every declared capability is legal for the declared roles: a
                                       gated (``REQUEST_*``) capability requires the ``EXECUTOR`` role.
  - ``tool_surface_safe``            — the tool surface passes the deny-list (``assert_agent_tools_safe``).
  - ``role_methods_present``         — the impl exposes a callable method for EACH declared role
                                       (``propose`` for PROPOSER, ``execute`` for EXECUTOR).
  - ``proposer_gate_readonly``       — if the module is a PROPOSER, its proposer-scoped ``GateFacade``
                                       raises ``CapabilityNotDeclared`` on EVERY gated method — the
                                       structural read/propose-only proof (holds even for a dual-role
                                       module, whose proposer lane strips gated capabilities).
  - ``gated_capabilities_serviced``  — every gated capability the manifest declares is serviced by a
                                       real ``GateFacade`` method (no orphan capability).
  - ``name_registerable``            — the manifest name is non-empty AND the module registers cleanly
                                       into a fresh registry (the full registration path succeeds).
  - ``required_tools_reachable``     — VT-669 SUFFICIENCY: every tool the manifest lists in
                                       ``required_tools`` is present in the ``tool_catalog`` AND is
                                       reachable — on the module's OWN ``tools`` surface OR in the
                                       Manager-scoped common READ set. Fails-loud (naming the missing
                                       tool) so a specialist that silently lacks a tool its job needs
                                       dies at boot, not at 10am. ``n/a`` for a module declaring no
                                       ``required_tools`` (so this check adds no heavy catalog import
                                       to the common empty case — the catalog is imported LAZILY, only
                                       when a module actually declares a required tool).

Heavy imports are LAZY (the deny-list guard pulls langchain via ``orchestrator.agent`` at RUNTIME)
so this module stays dep-less-smoke safe. A test that exercises the register()/facade paths should
``pytest.importorskip("langchain")`` (as the framework's own tests do).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from orchestrator.agent_framework.capabilities import (
    ROLE_METHOD,
    AgentRole,
    Capability,
)
from orchestrator.agent_framework.gate_facade import (
    GATED_METHOD_BY_CAPABILITY,
    CapabilityNotDeclared,
    GateFacade,
)
from orchestrator.agent_framework.manifest import AgentManifest, ManifestError


@dataclass(frozen=True)
class CheckResult:
    """The outcome of ONE named conformance check."""

    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        tail = f" — {self.detail}" if self.detail else ""
        return f"[{mark}] {self.name}{tail}"


@dataclass(frozen=True)
class ConformanceReport:
    """The full result of running the conformance suite against one module.

    ``passed`` is the AND of every check. Truthy iff conformant, so ``if check_module_conformance(m):``
    reads naturally. ``failures`` is the (possibly empty) tuple of failing checks, in check order.
    """

    module_name: str
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    def result(self, name: str) -> CheckResult:
        """The ``CheckResult`` for a named check. Raises ``KeyError`` if the check did not run."""
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(
            f"no conformance check named {name!r}; ran: {[r.name for r in self.results]}"
        )

    def __bool__(self) -> bool:
        return self.passed

    def __str__(self) -> str:
        head = f"conformance {self.module_name!r}: {'PASS' if self.passed else 'FAIL'}"
        return "\n".join([head, *(f"  {r}" for r in self.results)])


# --- individual checks -------------------------------------------------------------------------
#
# Each check is ``(module, manifest) -> (passed, detail)`` and NEVER raises for a normal failure
# (it returns ``False`` + a reason). ``check_module_conformance`` additionally wraps each call so an
# UNEXPECTED exception is also recorded as a failure — the suite as a whole raises nothing.


def _check_manifest_valid(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    try:
        manifest.validate()
    except ManifestError as exc:
        return False, str(exc)
    return True, ""


def _check_capabilities_legal_for_roles(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    bad_type = [c for c in manifest.capabilities if not isinstance(c, Capability)]
    if bad_type:
        return False, f"capabilities holds non-Capability values: {bad_type!r}"
    if manifest.gated_capabilities and AgentRole.EXECUTOR not in manifest.roles:
        return False, (
            f"gated capabilities {sorted(c.value for c in manifest.gated_capabilities)!r} require "
            f"the EXECUTOR role; roles={sorted(r.value for r in manifest.roles)!r}"
        )
    return True, ""


def _check_tool_surface_safe(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    # Lazy: the guard lives behind ``orchestrator.agent.__init__`` which eager-imports langchain.
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    try:
        assert_agent_tools_safe(manifest.tools, surface=f"agent_framework:{manifest.name}")
    except Exception as exc:  # noqa: BLE001 — ToolGuardrailViolation + any introspection failure
        return False, f"tool surface rejected by the deny-list guard: {exc}"
    return True, ""


def _check_role_methods_present(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    if not manifest.roles:
        return False, "manifest declares no roles"
    missing = []
    for role in manifest.roles:
        method = ROLE_METHOD.get(role)
        if method is None or not callable(getattr(module, method, None)):
            missing.append((role.value, method))
    if missing:
        return False, f"missing/uncallable role method(s): {missing!r}"
    return True, ""


def _check_proposer_gate_readonly(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    if AgentRole.PROPOSER not in manifest.roles:
        return True, "n/a: module has no PROPOSER role"
    # Build the SAME proposer-lane facade the framework would (role-scoped capabilities — gated
    # capabilities stripped for the proposer lane).
    facade = GateFacade(
        tenant_id=uuid4(),
        capabilities=manifest.capabilities_for_role(AgentRole.PROPOSER),
    )
    for cap, method_name in GATED_METHOD_BY_CAPABILITY.items():
        if facade.can(cap):
            # A proposer facade must NOT service a gated capability. Do NOT invoke it (that would
            # reach the real gate) — the structural leak IS the failure.
            return False, (
                f"proposer-scoped facade SERVICES gated capability {cap.value!r} (method "
                f"{method_name!r}) — a proposer lane must be structurally side-effect-free"
            )
        method = getattr(facade, method_name, None)
        if not callable(method):
            return False, f"GateFacade has no callable {method_name!r} for {cap.value!r}"
        try:
            _invoke_with_placeholders(method)
        except CapabilityNotDeclared:
            continue  # good — the door refused before reaching any gate
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"proposer facade {method_name!r} raised {type(exc).__name__} instead of "
                f"CapabilityNotDeclared: {exc}"
            )
        return False, f"proposer facade {method_name!r} did NOT raise CapabilityNotDeclared"
    return True, ""


def _check_gated_capabilities_serviced(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    orphans = []
    for cap in manifest.gated_capabilities:
        method_name = GATED_METHOD_BY_CAPABILITY.get(cap)
        if method_name is None or not callable(getattr(GateFacade, method_name, None)):
            orphans.append(cap.value)
    if orphans:
        return False, (
            f"declared gated capabilities with no servicing GateFacade method: {sorted(orphans)!r}"
        )
    return True, ""


def _tool_surface_names(tools: Any) -> set[str]:
    """The ``.name`` (or callable ``__name__``) of each object on a ``tools`` surface."""
    names: set[str] = set()
    for t in tools:
        name = getattr(t, "name", None)
        if not (isinstance(name, str) and name):
            name = getattr(t, "__name__", None)
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _check_required_tools_reachable(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    required = tuple(getattr(manifest, "required_tools", ()) or ())
    if not required:
        # The common case (reference plugin + existing modules): nothing required -> nothing to
        # verify, and — crucially — we do NOT import the tool catalog (heavy: it pulls every tool
        # surface). The catalog import is gated on a module ACTUALLY declaring a required tool.
        return True, "n/a: no required_tools declared"

    # Lazy: importing the catalog + the common surfaces pulls langchain (+ a constructed chat
    # model for the integration surface). Only reached when a module declares required_tools.
    from orchestrator.agent_framework.tool_catalog import catalog_tool_names
    from orchestrator.agent_framework.tools_common import (
        COMMON_ADVISORY_TOOLS,
        COMMON_READ_TOOLS,
    )

    catalog_names = catalog_tool_names()
    own_names = _tool_surface_names(manifest.tools)
    common_read_names = _tool_surface_names(COMMON_READ_TOOLS)
    # "reachable" = a tool the module holds itself OR a Manager-scoped common tool it is declared to
    # reach (ARCHITECTURE §1.1/§1.3 — the specialist pulls operational data via the Manager's reads;
    # VT-672 adds the common ADVISORY hand-backs, e.g. `escalate`).
    reachable = own_names | common_read_names | _tool_surface_names(COMMON_ADVISORY_TOOLS)

    not_in_catalog = sorted(t for t in required if t not in catalog_names)
    if not_in_catalog:
        return False, (
            f"required tool(s) not in the tool_catalog (unknown/typo'd tool — a required tool must "
            f"be a real cataloged tool): {not_in_catalog!r}"
        )
    unreachable = sorted(t for t in required if t not in reachable)
    if unreachable:
        return False, (
            f"required tool(s) not REACHABLE by module {manifest.name!r}: {unreachable!r} — none is "
            f"on the module's own tools surface {sorted(own_names)!r} nor in the Manager-scoped "
            f"common READ set {sorted(common_read_names)!r}. The specialist cannot actually do its "
            "job; provision the tool (hold it, or reach it through the Manager's common reads)."
        )
    return True, ""


def _check_name_registerable(module: Any, manifest: AgentManifest) -> tuple[bool, str]:
    if not manifest.name or not manifest.name.strip():
        return False, "manifest.name is empty/whitespace"
    # Lazy: register() pulls the deny-list guard (langchain) at runtime.
    from orchestrator.agent_framework.registration import (
        AgentFrameworkRegistry,
        ModuleRegistrationError,
    )

    reg = AgentFrameworkRegistry()
    try:
        reg.register(module)
    except ModuleRegistrationError as exc:
        return False, f"register() rejected the module: {exc}"
    return True, ""


#: The manifest-dependent checks, in run order. ``has_manifest`` is evaluated first + separately
#: (these cannot run without a manifest).
_CHECKS: tuple[tuple[str, Callable[[Any, AgentManifest], tuple[bool, str]]], ...] = (
    ("manifest_valid", _check_manifest_valid),
    ("capabilities_legal_for_roles", _check_capabilities_legal_for_roles),
    ("tool_surface_safe", _check_tool_surface_safe),
    ("role_methods_present", _check_role_methods_present),
    ("proposer_gate_readonly", _check_proposer_gate_readonly),
    ("gated_capabilities_serviced", _check_gated_capabilities_serviced),
    ("name_registerable", _check_name_registerable),
    ("required_tools_reachable", _check_required_tools_reachable),
)

#: The complete ordered set of check names a report carries (has_manifest + the manifest-dependent
#: checks). Exposed so a test can assert full coverage / iterate every check.
CHECK_NAMES: tuple[str, ...] = ("has_manifest", *(name for name, _ in _CHECKS))


def _invoke_with_placeholders(method: Callable[..., Any]) -> Any:
    """Call a bound facade method supplying a placeholder for each required positional parameter.

    Used ONLY on a proposer-scoped facade for a capability that is NOT serviced — so the method
    raises ``CapabilityNotDeclared`` in its capability guard BEFORE any placeholder value is read or
    any real gate is reached. Reflection over the signature keeps the harness from hard-coding each
    gated method's arity.
    """
    sig = inspect.signature(method)
    args: list[Any] = []
    for p in sig.parameters.values():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if p.default is inspect.Parameter.empty and p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            args.append(None)
    return method(*args)


def check_module_conformance(module: Any) -> ConformanceReport:
    """Run the full conformance suite against ``module``. PURE — returns a report, raises nothing.

    The report names each check with pass/fail + a detail string. A module is conformant iff every
    check passes (``report.passed`` / ``bool(report)``).
    """
    manifest = getattr(module, "manifest", None)
    has_manifest = isinstance(manifest, AgentManifest)
    module_name = manifest.name if has_manifest and manifest.name else repr(module)

    results: list[CheckResult] = [
        CheckResult(
            "has_manifest",
            has_manifest,
            "" if has_manifest else f"module {module!r} exposes no AgentManifest 'manifest'",
        )
    ]

    if not has_manifest:
        # The manifest-dependent checks cannot run — record each as failed with a clear reason so the
        # report still carries the full check set (stable shape for callers iterating CHECK_NAMES).
        for name, _ in _CHECKS:
            results.append(CheckResult(name, False, "skipped: module has no AgentManifest"))
        return ConformanceReport(module_name=module_name, results=tuple(results))

    for name, check in _CHECKS:
        try:
            passed, detail = check(module, manifest)
        except Exception as exc:  # noqa: BLE001 — conformance never raises; a crash IS a failure.
            passed, detail = False, f"check raised {type(exc).__name__}: {exc}"
        results.append(CheckResult(name, passed, detail))

    return ConformanceReport(module_name=module_name, results=tuple(results))


def assert_conforms(module: Any) -> ConformanceReport:
    """pytest helper: run the conformance suite and FAIL the test at the first violation.

    Returns the (passing) report on success, so a test may make further assertions on it. On
    failure it calls ``pytest.fail`` with the failing check's name + detail (falling back to
    ``AssertionError`` if pytest is somehow unavailable — this is meant to run under pytest).
    """
    report = check_module_conformance(module)
    if report.passed:
        return report
    first = report.failures[0]
    message = (
        f"module {report.module_name!r} FAILED conformance check {first.name!r}: {first.detail}\n"
        f"{report}"
    )
    try:
        import pytest
    except ImportError:  # pragma: no cover — assert_conforms is a test-time helper.
        raise AssertionError(message) from None
    pytest.fail(message, pytrace=False)


__all__ = [
    "CHECK_NAMES",
    "CheckResult",
    "ConformanceReport",
    "assert_conforms",
    "check_module_conformance",
]
