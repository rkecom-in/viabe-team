"""Orchestrator-agent package (VT-3.9).

The orchestrator-agent is the Stage-2 reasoning brain — it runs ON the LangGraph
substrate and decides routing for the residual ~30% of events the deterministic
Pre-Filter Gate hands up. Pillar 1: reasoning lives here, never in the
deterministic subtree; this package must not import the phase machine
(transitions / invariants) — CI enforces it.

CL-2026-07-16 — the three brain re-exports below are LAZY (PEP 562 ``__getattr__``).
Importing ``orchestrator_agent`` eagerly builds a module-level chat-model singleton
(``orchestrator_agent.py`` ``_MODEL = resolve_chat_model("complex")`` + the agent graph),
which requires the full LLM provider env at IMPORT time. That made a pure ``import
orchestrator.agent.<light-submodule>`` (e.g. ``emission_gate``, imported by the money-path
DB-assert harness) fail wherever the resolved provider's key is absent — the convo-harness
``assert_stated_count_matches_db`` crash under ``railway run`` (dev's "complex" tier is an
OpenAI model whose sealed key reads unset in the subprocess). Deferring the import to first
attribute access keeps submodule imports light without changing any call site: ``from
orchestrator.agent import build_orchestrator_agent`` still works, it just triggers the build
on access instead of at package import.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, Any

__all__ = [
    "ORCHESTRATOR_AGENT_SYSTEM_PROMPT",
    "build_orchestrator_agent",
    "orchestrator_agent",
]

if TYPE_CHECKING:  # eager only for type-checkers, never at runtime
    from orchestrator.agent.orchestrator_agent import (
        ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
        build_orchestrator_agent,
        orchestrator_agent,
    )


def __getattr__(name: str) -> Any:
    """PEP 562 lazy re-export — imports orchestrator_agent (and its eager model build) only when
    one of the brain symbols is actually accessed, not at package import time.

    NOTE the ``orchestrator_agent`` name collision: it is BOTH a submodule AND the re-exported
    singleton INSTANCE. We import the submodule via ``importlib.import_module`` (a full dotted-path
    import that bypasses this package ``__getattr__`` — a ``from orchestrator.agent import
    orchestrator_agent`` here would recurse) and then bind ALL three symbols onto the package,
    overwriting the submodule shadow of ``orchestrator_agent`` with the INSTANCE exactly as the old
    eager ``from orchestrator.agent.orchestrator_agent import (...)`` did. One-time; subsequent
    accesses hit the real attributes and never re-enter this hook."""
    if name in __all__:
        oa = importlib.import_module("orchestrator.agent.orchestrator_agent")
        pkg = sys.modules[__name__]
        for sym in __all__:
            setattr(pkg, sym, getattr(oa, sym))
        return getattr(pkg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
