"""VT-650 — tests for the module conformance harness (THE simple verification process).

Proves the harness both ways:
  - a CONFORMANT module (the reference plugin + a purpose-built dual-role fixture) passes EVERY
    named check, and ``assert_conforms`` returns cleanly;
  - for each named check there is an INTENTIONALLY-broken module that makes exactly that check FAIL
    (and ``check_module_conformance`` NEVER raises — a crash is recorded as a failure, not thrown).

The harness reaches register()/deny-list paths (langchain via ``orchestrator.agent``), so the whole
module is skipped in the dep-less smoke, per the repo dep-less discipline.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("langchain")

import orchestrator.agent_framework.gate_facade as gate_facade  # noqa: E402
from orchestrator.agent_framework import (  # noqa: E402
    AgentBrief,
    AgentManifest,
    AgentRole,
    Capability,
    CheckResult,
    ConformanceReport,
    assert_conforms,
    check_module_conformance,
)
from orchestrator.agent_framework.conformance import CHECK_NAMES  # noqa: E402
from orchestrator.agent_framework.reference_plugin import BusinessContextReader  # noqa: E402


def _fake_bc(_tid=None):
    return types.SimpleNamespace(objective={"goal": "grow"}, identity={"name": "Biz"})


# --- conforming fixtures -----------------------------------------------------------------------


class _ConformingDualRole:
    """A valid {PROPOSER, EXECUTOR} module (the SR shape) — must pass EVERY check."""

    manifest = AgentManifest(
        name="conforming_dual_role",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),
        description="proposes AND executes; declares a gated capability (legal — EXECUTOR present)",
        capabilities=frozenset(
            {Capability.READ_BUSINESS_CONTEXT, Capability.REQUEST_CUSTOMER_SEND}
        ),
        # VT-686: a fully taxonomy-complete manifest — this fixture must pass EVERY check,
        # brief_complete included.
        category="Tech",
        tags=frozenset({"fixture"}),
        brief=AgentBrief(
            what_it_does="a conformance-harness test fixture",
            actions=("propose", "execute"),
            business_activities=("prove the harness passes a well-formed dual-role module",),
            when_to_use="never — test fixture only",
            limits=("not a real agent — test double",),
        ),
    )

    def propose(self, ctx, gate):  # pragma: no cover - not invoked by the harness
        ...

    def execute(self, ctx, gate):  # pragma: no cover - not invoked by the harness
        ...


def test_reference_plugin_conforms():
    report = check_module_conformance(BusinessContextReader(reader=_fake_bc))
    assert report.passed, str(report)
    # the report carries EVERY named check, in order.
    assert tuple(r.name for r in report.results) == CHECK_NAMES


def test_dual_role_fixture_conforms():
    report = check_module_conformance(_ConformingDualRole())
    assert report.passed, str(report)


def test_assert_conforms_returns_report_on_pass():
    report = assert_conforms(BusinessContextReader(reader=_fake_bc))
    assert isinstance(report, ConformanceReport)
    assert report.passed


def test_check_result_and_report_shapes():
    report = check_module_conformance(_ConformingDualRole())
    assert isinstance(report.results[0], CheckResult)
    assert bool(report) is True
    assert report.failures == ()
    # named lookup works + raises for an unknown check
    assert report.result("manifest_valid").passed is True
    with pytest.raises(KeyError):
        report.result("no_such_check")


# --- purity: the harness raises NOTHING --------------------------------------------------------


def test_check_module_conformance_never_raises_on_garbage():
    """A non-module input yields a report (has_manifest fails), not an exception."""
    report = check_module_conformance(object())
    assert report.result("has_manifest").passed is False
    assert not report.passed
    # the manifest-dependent checks are still present (stable shape), recorded as failed.
    assert tuple(r.name for r in report.results) == CHECK_NAMES


# --- one intentionally-broken module PER CHECK (prove each fires) -------------------------------


def test_fail_has_manifest():
    report = check_module_conformance(object())
    assert report.result("has_manifest").passed is False


def test_fail_manifest_valid():
    class _BadVersion:
        manifest = AgentManifest(
            name="bad_version",
            version="",  # empty -> validate() raises
            roles=frozenset({AgentRole.EXECUTOR}),
            description="x",
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_BadVersion())
    assert report.result("manifest_valid").passed is False


def test_fail_capabilities_legal_for_roles():
    class _PureProposerGated:
        manifest = AgentManifest(
            name="pure_proposer_gated",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="pure proposer illegally declaring a gated capability",
            capabilities=frozenset({Capability.REQUEST_CUSTOMER_SEND}),
        )

        def propose(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_PureProposerGated())
    assert report.result("capabilities_legal_for_roles").passed is False


def test_fail_tool_surface_safe():
    class _ForbiddenTool:
        manifest = AgentManifest(
            name="forbidden_tool",
            version="1.0.0",
            roles=frozenset({AgentRole.EXECUTOR}),
            description="holds a send tool it must not have",
            capabilities=frozenset(),
            tools=(types.SimpleNamespace(name="send_whatsapp_message"),),
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_ForbiddenTool())
    assert report.result("tool_surface_safe").passed is False


def test_fail_role_methods_present():
    class _MissingPropose:
        manifest = AgentManifest(
            name="missing_propose",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="declares PROPOSER but exposes no propose",
            capabilities=frozenset(),
        )

        def execute(self, ctx, gate):  # pragma: no cover - wrong method for the role
            ...

    report = check_module_conformance(_MissingPropose())
    assert report.result("role_methods_present").passed is False


def test_fail_proposer_gate_readonly():
    """A dual-role module whose manifest LEAKS a gated capability into the proposer lane — the
    proposer-scoped facade would service a send. The check catches the structural leak (without
    ever invoking the gate)."""

    class _LeakyManifest(AgentManifest):
        def capabilities_for_role(self, role):  # noqa: ARG002 - BUG: never strips gated caps
            return frozenset(self.capabilities)

    class _LeakyProposer:
        manifest = _LeakyManifest(
            name="leaky_proposer",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),
            description="proposer facade leaks a gated capability",
            capabilities=frozenset(
                {Capability.READ_BUSINESS_CONTEXT, Capability.REQUEST_CUSTOMER_SEND}
            ),
        )

        def propose(self, ctx, gate):  # pragma: no cover
            ...

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_LeakyProposer())
    # the isolated target: manifest is valid + capabilities legal, but the proposer lane leaks.
    assert report.result("manifest_valid").passed is True
    assert report.result("capabilities_legal_for_roles").passed is True
    assert report.result("proposer_gate_readonly").passed is False


def test_fail_gated_capabilities_serviced(monkeypatch):
    """Simulate an ORPHAN gated capability: a gated capability with no servicing facade method
    (as if it were added to GATED_CAPABILITIES but never wired a door)."""
    monkeypatch.delitem(gate_facade.GATED_METHOD_BY_CAPABILITY, Capability.REQUEST_CUSTOMER_SEND)

    class _OrphanExec:
        manifest = AgentManifest(
            name="orphan_exec",
            version="1.0.0",
            roles=frozenset({AgentRole.EXECUTOR}),
            description="declares a gated capability whose facade door was removed",
            capabilities=frozenset({Capability.REQUEST_CUSTOMER_SEND}),
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_OrphanExec())
    assert report.result("gated_capabilities_serviced").passed is False


def test_fail_name_registerable():
    class _EmptyName:
        manifest = AgentManifest(
            name="   ",  # whitespace-only
            version="1.0.0",
            roles=frozenset({AgentRole.EXECUTOR}),
            description="x",
            capabilities=frozenset(),
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_EmptyName())
    assert report.result("name_registerable").passed is False


# --- assert_conforms fails the test at the first violation -------------------------------------


def test_assert_conforms_fails_on_broken_module():
    class _MissingPropose:
        manifest = AgentManifest(
            name="assert_missing_propose",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="no propose method",
            capabilities=frozenset(),
        )

        def execute(self, ctx, gate):  # pragma: no cover
            ...

    try:
        assert_conforms(_MissingPropose())
    except BaseException as exc:  # noqa: BLE001 - pytest.fail raises a BaseException (Failed)
        assert "conformance check" in str(exc)
        assert "role_methods_present" in str(exc)
    else:  # pragma: no cover - assert_conforms MUST fail here
        pytest.fail("assert_conforms did not fail on a broken module")
