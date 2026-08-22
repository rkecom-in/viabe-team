"""Structural backstops for agents whose product is an artifact, never an effect.

The capability guard is the primary boundary.  This AST scan is defence in depth: it catches a
future direct transport/ads import close to the change that introduced it.  It deliberately scans
imports, not arbitrary words in comments or copy.
"""

from __future__ import annotations

import ast
from pathlib import Path


class SendlessImportViolation(RuntimeError):
    """Raised when an artifact-only agent imports an effect-adjacent module."""


_FORBIDDEN_IMPORT_PREFIXES = (
    "facebook_business",
    "google.ads",
    "httpx",
    "requests",
    "resend",
    "twilio",
    "urllib.request",
    "orchestrator.agent.customer_send",
    "orchestrator.agent.tools.send_whatsapp",
    "orchestrator.agents.business_impact_choke",
    "orchestrator.integrations.twilio_send",
)


def forbidden_imports(source: str) -> tuple[str, ...]:
    """Return forbidden imported module names from Python ``source``."""

    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return tuple(
        name
        for name in imported
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES)
    )


def assert_source_sendless(source: str, *, surface: str) -> None:
    """Fail closed when source directly imports a send/spend/publish transport."""

    hits = forbidden_imports(source)
    if hits:
        raise SendlessImportViolation(
            f"artifact-only surface {surface!r} imports effect-adjacent module(s): {hits!r}"
        )


def assert_file_sendless(path: str | Path, *, surface: str) -> None:
    assert_source_sendless(Path(path).read_text(encoding="utf-8"), surface=surface)


__all__ = [
    "SendlessImportViolation",
    "assert_file_sendless",
    "assert_source_sendless",
    "forbidden_imports",
]
