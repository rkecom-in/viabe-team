"""VT-686 — unit tests for the ``AgentManifest`` taxonomy fields: ``category`` / ``tags`` / ``brief``.

Proves the back-compat + shape-enforcement contract ``manifest.py`` documents:
  - the three fields default to SAFE, back-compat values (``""`` / ``frozenset()`` / ``None``) and a
    manifest carrying only the defaults still ``validate()``s clean (an un-migrated module boots);
  - the moment a value is SUPPLIED (non-default), ``validate()`` enforces its SHAPE: a ``category``
    must be a real ``AGENT_CATEGORIES`` member, ``tags`` must be lowercase/non-empty/space-free, and
    ``brief`` must be an ``AgentBrief`` instance (not a dict/string by mistake).
  - REQUIREDNESS (every field non-empty, ``brief`` fully populated) is a SEPARATE, stricter layer —
    the conformance ``brief_complete`` check (see ``test_conformance_brief_complete.py``), not
    ``validate()``. This file only proves the dataclass/validate() half.

No heavy deps: ``manifest.py`` (and everything it imports) is dep-less-smoke safe — no
``importorskip`` needed here.
"""

from __future__ import annotations

import pytest

from orchestrator.agent_framework import (
    AGENT_CATEGORIES,
    AgentBrief,
    AgentManifest,
    AgentRole,
    ManifestError,
)


def _manifest(**overrides):
    base = dict(
        name="taxonomy_probe",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="a manifest fixture for taxonomy-field tests",
    )
    base.update(overrides)
    return AgentManifest(**base)


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


# --- AGENT_CATEGORIES shape ----------------------------------------------------------------------


def test_agent_categories_is_the_expected_finite_set():
    assert AGENT_CATEGORIES == {
        "Compliance",
        "Sales",
        "Marketing",
        "Finance",
        "Accounting",
        "Onboarding",
        "Integration",
        "Tech",
        "CostOpt",
    }
    assert isinstance(AGENT_CATEGORIES, frozenset)


# --- back-compat: the safe defaults validate clean ------------------------------------------------


def test_defaults_are_back_compat_safe():
    """A manifest carrying NONE of the new fields (an un-migrated module) still validates clean."""
    m = _manifest()
    assert m.category == ""
    assert m.tags == frozenset()
    assert m.brief is None
    m.validate()  # must not raise


# --- category: validated ONLY when supplied -------------------------------------------------------


def test_valid_category_passes():
    m = _manifest(category="Compliance")
    m.validate()  # must not raise


def test_unrecognized_category_boot_fails():
    m = _manifest(category="NotARealCategory")
    with pytest.raises(ManifestError, match="category"):
        m.validate()


@pytest.mark.parametrize("category", sorted(AGENT_CATEGORIES))
def test_every_declared_category_is_accepted(category):
    _manifest(category=category).validate()


# --- tags: shape-only (lowercase, non-empty, no whitespace) ----------------------------------------


def test_valid_tags_pass():
    m = _manifest(tags=frozenset({"gst", "gstr1", "filing-readiness"}))
    m.validate()  # must not raise


def test_uppercase_tag_rejected():
    m = _manifest(tags=frozenset({"GST"}))
    with pytest.raises(ManifestError, match="tags"):
        m.validate()


def test_tag_with_space_rejected():
    m = _manifest(tags=frozenset({"gst returns"}))
    with pytest.raises(ManifestError, match="tags"):
        m.validate()


def test_empty_string_tag_rejected():
    m = _manifest(tags=frozenset({""}))
    with pytest.raises(ManifestError, match="tags"):
        m.validate()


# --- brief: type-checked only when supplied ---------------------------------------------------


def test_valid_brief_passes():
    m = _manifest(brief=_brief())
    m.validate()  # must not raise


def test_brief_wrong_type_rejected():
    m = _manifest(brief={"what_it_does": "not a real AgentBrief"})
    with pytest.raises(ManifestError, match="brief"):
        m.validate()


def test_brief_none_is_the_default_and_valid():
    m = _manifest(brief=None)
    m.validate()  # must not raise — None is the back-compat default


# --- AgentBrief itself: a plain, frozen, structured value object ----------------------------------


def test_agent_brief_is_frozen():
    brief = _brief()
    with pytest.raises((AttributeError, TypeError)):
        brief.what_it_does = "mutated"  # type: ignore[misc]


def test_agent_brief_fields_round_trip():
    brief = _brief(
        actions=("read_x", "write_y"),
        business_activities=("outcome one", "outcome two"),
        limits=("does not do z", "does not do w"),
    )
    assert brief.actions == ("read_x", "write_y")
    assert brief.business_activities == ("outcome one", "outcome two")
    assert brief.limits == ("does not do z", "does not do w")
