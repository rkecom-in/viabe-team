"""VT-686 — ``render_agent_directory``: the Manager-facing agent DIRECTORY.

WHAT THIS IS
------------
A pure, deterministic renderer that turns an ``AgentFrameworkRegistry`` into a compact set of
per-agent "identity cards" — category / tags / what-it-does / when-to-use / limits — so the Manager
grounds a delegation decision in each agent's own declared ``AgentBrief`` (``agent_framework.
manifest.AgentBrief``) instead of a spawn-tool docstring. This is the rendering half of VT-686; the
context-wiring half (``agent/dispatch.py``'s ``_build_agent_directory_block``) reads the DEFAULT
registry and inserts this text as a per-turn ``SystemMessage``, riding the same volatile-block
family as the VT-681 capability-truth block (VT-194 cache holds — see that module's docstring).

WHY A MODULE IS SKIPPED, NOT PARTIALLY RENDERED
------------------------------------------------
``render_agent_directory`` cards ONLY modules whose manifest is VT-686-COMPLETE (a real
``category`` + at least one ``tag`` + a fully-populated ``AgentBrief`` — the same bar the
``brief_complete`` conformance check enforces). A module still carrying the back-compat DEFAULTS
(``category=""``, ``tags=frozenset()``, ``brief=None``) contributes NOTHING to the directory rather
than a garbled half-card — this is the retrofit-safe posture: an un-migrated module is invisible to
the directory (the Manager simply doesn't get a card for it) instead of rendering a broken one.

DETERMINISM + SAFETY
---------------------
Cards are ordered by the registry's own ``names()`` (already sorted — see ``registration.
AgentFrameworkRegistry.names``), so the rendered text is STABLE for a given registry state (no
dict-iteration-order flakiness, no timestamp). Each card is a handful of short lines: a header
(name/category/tags), a "What" line, a "When" line, and a "Does NOT" line — no tool dumps, no raw
capability enums, no PII (the brief is authored prose, never a data read).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.agent_framework.manifest import AgentManifest

if TYPE_CHECKING:  # pragma: no cover - type-checking only, avoids a runtime import cycle
    from orchestrator.agent_framework.registration import AgentFrameworkRegistry


def _render_card(manifest: AgentManifest) -> str | None:
    """One agent's identity card, or ``None`` if its manifest isn't VT-686-complete yet.

    "Complete" mirrors the ``brief_complete`` conformance bar: a real ``category``, at least one
    ``tag``, and an ``AgentBrief`` whose fields are all non-empty. A manifest still holding the
    back-compat defaults renders nothing (see the module docstring).
    """
    brief = manifest.brief
    if (
        not manifest.category
        or not manifest.tags
        or brief is None
        or not brief.what_it_does
        or not brief.when_to_use
        or not brief.limits
    ):
        return None
    tags = ", ".join(sorted(manifest.tags))
    return "\n".join(
        [
            f"### {manifest.name} [{manifest.category}] tags: {tags}",
            f"What: {brief.what_it_does}",
            f"When: {brief.when_to_use}",
            f"Does NOT: {'; '.join(brief.limits)}",
        ]
    )


def render_agent_directory(registry: AgentFrameworkRegistry) -> str:
    """Render every VT-686-complete module in ``registry`` as a compact Manager-facing directory.

    Deterministic ordering (``registry.names()`` — already sorted). Skips any module whose manifest
    is not yet VT-686-complete (see ``_render_card``) — an empty/all-skipped registry renders the
    empty string, never ``None`` (the caller, ``agent/dispatch.py``'s ``_build_agent_directory_block``,
    is the one that maps "" -> None for the per-turn SystemMessage insertion).
    """
    cards: list[str] = []
    for name in registry.names():
        try:
            manifest = registry.get(name).manifest
        except KeyError:  # pragma: no cover - names()/get() read the same store; defensive only
            continue
        card = _render_card(manifest)
        if card:
            cards.append(card)
    return "\n\n".join(cards)


__all__ = ["render_agent_directory"]
