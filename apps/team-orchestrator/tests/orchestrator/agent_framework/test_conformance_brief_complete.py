"""VT-686 — unit tests for conformance check #10, ``brief_complete``.

Proves the REQUIREDNESS half of the taxonomy: ``manifest.validate()`` only enforces SHAPE when a
field is supplied (see ``test_manifest.py``), but ``brief_complete`` makes category/tags/brief
REQUIRED for a module that wants to pass conformance — a module still carrying the back-compat
defaults FAILS this check (even though it validates + registers clean). One intentionally-broken
fixture per failure mode, mirroring ``tests/agent/test_conformance.py``'s existing style exactly.

Dep discipline: ``check_module_conformance``/``assert_conforms`` reach the deny-list guard
(langchain via ``orchestrator.agent.__init__``) through the ``name_registerable`` check. We
``importorskip("langchain")`` so the dep-less smoke skips this module; the full suite runs it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")

from orchestrator.agent_framework import (  # noqa: E402
    AgentBrief,
    AgentManifest,
    AgentRole,
    assert_conforms,
    check_module_conformance,
)


def _brief(**overrides):
    base = dict(
        what_it_does="does a thing",
        actions=("do_a_thing",),
        business_activities=("get a thing done",),
        when_to_use="when the owner asks for a thing",
        limits=("does not do other things",),
    )
    base.update(overrides)
    return AgentBrief(**base)


class _CompleteModule:
    """A fully VT-686-complete module: valid category, >=1 tag, a fully-populated brief."""

    manifest = AgentManifest(
        name="complete_module",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="a fully taxonomy-complete module",
        category="Sales",
        tags=frozenset({"winback"}),
        brief=_brief(),
    )

    def propose(self, ctx, gate):  # pragma: no cover - not invoked by the harness
        ...


def test_complete_module_passes_brief_complete():
    report = check_module_conformance(_CompleteModule())
    assert report.result("brief_complete").passed is True, str(report)
    assert report.passed, str(report)


def test_complete_module_passes_assert_conforms():
    assert_conforms(_CompleteModule())  # must not raise/fail


# --- failure modes: one fixture per missing piece -------------------------------------------------


class _NoCategoryModule:
    """Back-compat default category ("") — never supplied. brief_complete fails; validate() still
    passes (category is only shape-checked when non-default)."""

    manifest = AgentManifest(
        name="no_category_module",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="x",
        tags=frozenset({"a-tag"}),
        brief=_brief(),
    )

    def propose(self, ctx, gate):  # pragma: no cover
        ...


def test_fail_missing_category():
    report = check_module_conformance(_NoCategoryModule())
    assert report.result("manifest_valid").passed is True  # empty category is a legal default
    assert report.result("brief_complete").passed is False
    assert "category" in report.result("brief_complete").detail


class _NoTagsModule:
    """Back-compat default tags (frozenset()) — never supplied."""

    manifest = AgentManifest(
        name="no_tags_module",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="x",
        category="Tech",
        brief=_brief(),
    )

    def propose(self, ctx, gate):  # pragma: no cover
        ...


def test_fail_missing_tags():
    report = check_module_conformance(_NoTagsModule())
    assert report.result("manifest_valid").passed is True
    assert report.result("brief_complete").passed is False
    assert "tags" in report.result("brief_complete").detail


class _NoBriefModule:
    """Back-compat default brief (None) — never supplied."""

    manifest = AgentManifest(
        name="no_brief_module",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="x",
        category="Tech",
        tags=frozenset({"a-tag"}),
    )

    def propose(self, ctx, gate):  # pragma: no cover
        ...


def test_fail_missing_brief():
    report = check_module_conformance(_NoBriefModule())
    assert report.result("manifest_valid").passed is True
    assert report.result("brief_complete").passed is False
    assert "brief" in report.result("brief_complete").detail


def _module_with_brief(brief: AgentBrief):
    class _M:
        manifest = AgentManifest(
            name="partial_brief_module",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="x",
            category="Tech",
            tags=frozenset({"a-tag"}),
            brief=brief,
        )

        def propose(self, ctx, gate):  # pragma: no cover
            ...

    return _M()


def test_fail_empty_what_it_does():
    report = check_module_conformance(_module_with_brief(_brief(what_it_does="")))
    assert report.result("brief_complete").passed is False
    assert "what_it_does" in report.result("brief_complete").detail


def test_fail_empty_actions():
    report = check_module_conformance(_module_with_brief(_brief(actions=())))
    assert report.result("brief_complete").passed is False
    assert "actions" in report.result("brief_complete").detail


def test_fail_empty_business_activities():
    report = check_module_conformance(_module_with_brief(_brief(business_activities=())))
    assert report.result("brief_complete").passed is False
    assert "business_activities" in report.result("brief_complete").detail


def test_fail_empty_when_to_use():
    report = check_module_conformance(_module_with_brief(_brief(when_to_use="")))
    assert report.result("brief_complete").passed is False
    assert "when_to_use" in report.result("brief_complete").detail


def test_fail_no_limits_claimed():
    """The explicit, named case from the VT-686 design: an agent that claims NO limits fails."""
    report = check_module_conformance(_module_with_brief(_brief(limits=())))
    assert report.result("brief_complete").passed is False
    assert "limits" in report.result("brief_complete").detail


def test_fail_unrecognized_category_at_conformance_layer():
    """A category not in AGENT_CATEGORIES fails BOTH ``manifest_valid`` (validate() boot-fails) AND
    ``brief_complete`` (independently re-checks category membership)."""

    class _BadCategory:
        manifest = AgentManifest(
            name="bad_category_module",
            version="1.0.0",
            roles=frozenset({AgentRole.PROPOSER}),
            description="x",
            category="NotARealCategory",
            tags=frozenset({"a-tag"}),
            brief=_brief(),
        )

        def propose(self, ctx, gate):  # pragma: no cover
            ...

    report = check_module_conformance(_BadCategory())
    assert report.result("manifest_valid").passed is False
    assert report.result("brief_complete").passed is False


def test_assert_conforms_fails_on_brief_complete_violation():
    try:
        assert_conforms(_NoBriefModule())
    except BaseException as exc:  # noqa: BLE001 - pytest.fail raises a BaseException (Failed)
        assert "brief_complete" in str(exc)
    else:  # pragma: no cover - assert_conforms MUST fail here
        pytest.fail("assert_conforms did not fail on a brief-incomplete module")
