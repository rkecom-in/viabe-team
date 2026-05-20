"""Central tool registry (VT-39).

Each tool subtask (VT-5.2 ... VT-5.13) registers its tool class HERE.
The agent SDK's tool list is built from this registry at agent
construction time, filtered by the specialist's ``tool_subset``.

Today the registry is empty — no individual tools exist on `main` yet
(the framework lands first per brief). When VT-5.2 onwards begin
landing, each PR adds one ``register()`` call.

LLM-backed audit: ``llm_backed_in_subset`` returns the LLM-backed
tools in a given subset. Used by:

  - the system-prompt generator (VT-33) to enumerate which tools are
    Opus-backed
  - the cost-budget pre-flight (per-run estimate)
  - the rationale doc's "currently locked LLM-backed tools" appendix
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from team_shared.mcp import MCPTool


_REGISTRY: dict[str, type[MCPTool[Any, Any]]] = {}


class ToolNameCollision(RuntimeError):
    """Raised when two tools register the same ``name``."""


def register(tool_cls: type[MCPTool[Any, Any]]) -> type[MCPTool[Any, Any]]:
    """Register a tool class under its declared ``name``. Callable as
    a class decorator OR a direct call. Idempotent against re-import:
    a second registration of the same class under the same name is a
    no-op; a different class under the same name raises."""
    name = tool_cls.name
    existing = _REGISTRY.get(name)
    if existing is tool_cls:
        return tool_cls
    if existing is not None:
        raise ToolNameCollision(
            f"tool name {name!r} already registered as {existing.__name__}; "
            f"cannot re-register as {tool_cls.__name__}"
        )
    _REGISTRY[name] = tool_cls
    return tool_cls


def get(name: str) -> type[MCPTool[Any, Any]]:
    """Return the registered tool class for ``name``. Raises KeyError
    when absent — callers should validate their subset first via
    ``validate_subset`` to surface unknowns as a single error."""
    return _REGISTRY[name]


def all_tool_names() -> list[str]:
    """All registered tool names, sorted for stable telemetry / docs."""
    return sorted(_REGISTRY)


def validate_subset(subset: Iterable[str]) -> list[str]:
    """Return the list of names in ``subset`` that are NOT registered.
    Empty list means the subset is fully resolvable. Callers raise on
    a non-empty return."""
    return [n for n in subset if n not in _REGISTRY]


def llm_backed_in_subset(subset: Iterable[str]) -> list[str]:
    """Return the subset of ``subset`` whose registered tool class has
    ``is_llm_backed() == True``. Sorted. Used by the prompt generator
    + cost pre-flight + the rationale-doc audit appendix.

    A name not in the registry is silently skipped — callers should
    call ``validate_subset`` first to surface unknowns separately."""
    out: list[str] = []
    for name in subset:
        cls = _REGISTRY.get(name)
        if cls is not None and cls.is_llm_backed():
            out.append(name)
    return sorted(out)


def _reset_for_tests() -> None:
    """Clear the registry. Tests call this in their setup to isolate."""
    _REGISTRY.clear()


__all__ = [
    "ToolNameCollision",
    "all_tool_names",
    "get",
    "llm_backed_in_subset",
    "register",
    "validate_subset",
]
