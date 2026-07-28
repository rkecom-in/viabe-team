"""Fail-closed guards for tenant-free O8 global registry payloads.

This module stays at the dependency-free orchestrator boundary so ingestion and prior-promotion
gates can use it without importing the database-backed ``orchestrator.knowledge`` package.
Global tables structurally omit ``tenant_id``, but a caller could still place a tenant UUID or
other uniquely identifying token inside a claim/note/JSON value.  Such content must remain in
tenant scope; it is rejected rather than silently redacted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class GlobalKnowledgePurityError(ValueError):
    """A tenant identifier was found in content intended for the global registry."""


def _text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _text_values(nested)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            yield from _text_values(nested)


def assert_global_payload_pure(
    payload: Any, *, tenant_identifiers: Iterable[str]
) -> None:
    """Reject a global payload containing any supplied tenant-unique identifier.

    Identifiers are compared case-insensitively as substrings because they may sit inside prose or
    JSON. Empty identifiers are ignored instead of matching every string.
    """

    identifiers = tuple(
        dict.fromkeys(value.strip().casefold() for value in tenant_identifiers if value.strip())
    )
    if not identifiers:
        return
    for text in _text_values(payload):
        folded = text.casefold()
        for identifier in identifiers:
            if identifier in folded:
                raise GlobalKnowledgePurityError(
                    "tenant identifier detected in global knowledge payload"
                )


__all__ = ["GlobalKnowledgePurityError", "assert_global_payload_pure"]
