"""VT-686 — unit tests for ``render_agent_directory`` (``agent_framework/directory.py``).

Proves the renderer is:
  - DETERMINISTIC — same input, same output, ordered by name (not registration/dict order);
  - CONTENT-ACCURATE — each card carries name/category/tags/what/when/limits and NOTHING else (no
    tool dumps, no raw capability enums, no PII);
  - RETROFIT-SAFE — a module still holding the back-compat taxonomy defaults (category="" / no
    tags / brief=None) is SKIPPED entirely rather than rendering a garbled half-card;
  - total-miss-safe — an empty registry (or one with only incomplete modules) renders "".

No heavy deps needed: ``AgentManifest``/``AgentBrief`` are dep-less-smoke safe, and this test uses a
plain duck-typed fake registry (``.names()`` + ``.get(name).manifest``) instead of the real
``AgentFrameworkRegistry.register()`` path — that path lazily pulls the langchain deny-list guard,
which this pure-renderer test has no need to exercise.
"""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.agent_framework import AgentBrief, AgentManifest, AgentRole
from orchestrator.agent_framework.directory import render_agent_directory


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


def _manifest(name: str, **overrides):
    base = dict(
        name=name,
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="x",
    )
    base.update(overrides)
    return AgentManifest(**base)


class _FakeRegistry:
    """A minimal duck-typed stand-in for ``AgentFrameworkRegistry`` — only what the renderer uses."""

    def __init__(self, manifests: dict[str, AgentManifest]) -> None:
        self._entries = {name: SimpleNamespace(manifest=m) for name, m in manifests.items()}

    def names(self) -> list[str]:
        return sorted(self._entries)

    def get(self, name: str):
        return self._entries[name]


# --- empty / all-skipped registries ---------------------------------------------------------------


def test_empty_registry_renders_empty_string():
    assert render_agent_directory(_FakeRegistry({})) == ""


def test_registry_of_only_incomplete_modules_renders_empty_string():
    """A module still carrying the back-compat defaults contributes NOTHING (not a half-card)."""
    incomplete = _manifest("bare_module")  # no category/tags/brief
    assert render_agent_directory(_FakeRegistry({"bare_module": incomplete})) == ""


# --- one complete module: content-accurate --------------------------------------------------------


def test_single_complete_module_card_content():
    m = _manifest(
        "sales_recovery",
        category="Sales",
        tags=frozenset({"winback", "lapsed"}),
        brief=_brief(
            what_it_does="Wins back lapsed customers.",
            when_to_use="Route here for lapsed-customer asks.",
            limits=("does not send directly — arms the approval",),
        ),
    )
    text = render_agent_directory(_FakeRegistry({"sales_recovery": m}))

    assert text.startswith("### sales_recovery [Sales] tags: lapsed, winback")
    assert "What: Wins back lapsed customers." in text
    assert "When: Route here for lapsed-customer asks." in text
    assert "Does NOT: does not send directly — arms the approval" in text
    # no tool dumps, no raw capability enum leakage.
    assert "Capability." not in text
    assert "tools=" not in text


def test_tags_render_sorted_and_comma_joined():
    m = _manifest(
        "x",
        category="Tech",
        tags=frozenset({"z-tag", "a-tag", "m-tag"}),
        brief=_brief(),
    )
    text = render_agent_directory(_FakeRegistry({"x": m}))
    assert "tags: a-tag, m-tag, z-tag" in text


# --- multiple modules: deterministic ordering + partial-skip --------------------------------------


def test_multiple_modules_ordered_by_name_and_skips_incomplete():
    complete_b = _manifest("b_module", category="Finance", tags=frozenset({"cash"}), brief=_brief())
    complete_a = _manifest("a_module", category="Sales", tags=frozenset({"winback"}), brief=_brief())
    incomplete = _manifest("z_incomplete")  # skipped — no category/tags/brief

    registry = _FakeRegistry(
        {"b_module": complete_b, "a_module": complete_a, "z_incomplete": incomplete}
    )
    text = render_agent_directory(registry)

    a_pos = text.index("### a_module")
    b_pos = text.index("### b_module")
    assert a_pos < b_pos  # alphabetical, not insertion order
    assert "z_incomplete" not in text


def test_rendering_is_deterministic_across_calls():
    m1 = _manifest("m1", category="Sales", tags=frozenset({"winback"}), brief=_brief())
    m2 = _manifest("m2", category="Finance", tags=frozenset({"cash"}), brief=_brief())
    registry = _FakeRegistry({"m2": m2, "m1": m1})

    first = render_agent_directory(registry)
    second = render_agent_directory(registry)
    assert first == second


# --- partial-brief modules are ALSO skipped (not just brief=None) ---------------------------------


def test_module_with_brief_missing_limits_is_skipped():
    """A module whose brief is present but has an empty ``limits`` tuple is not yet fully complete
    (mirrors the conformance ``brief_complete`` bar) — the directory skips it too."""
    m = _manifest(
        "no_limits_module",
        category="Sales",
        tags=frozenset({"winback"}),
        brief=_brief(limits=()),
    )
    assert render_agent_directory(_FakeRegistry({"no_limits_module": m})) == ""


def test_module_with_no_tags_is_skipped():
    m = _manifest("no_tags_module", category="Sales", tags=frozenset(), brief=_brief())
    assert render_agent_directory(_FakeRegistry({"no_tags_module": m})) == ""
