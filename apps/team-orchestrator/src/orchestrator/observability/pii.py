"""PII redaction at observability sinks (VT-101 → VT-104 consolidation).

Thin delegation layer over the canonical
:mod:`orchestrator.privacy.pii_redactor`. Preserved as a re-export shim
so call sites threaded through VT-101 / VT-102 (the LangSmith
``@traceable`` decorator and the ``pipeline_log`` writer) keep their
public API and output byte-identical post-consolidation — that is the
canary's Group A regression contract.

Token format for named-key redaction (``phone``, ``customer_name``,
``body``, etc.) is preserved exactly so VT-101's LangSmith trace JSON
and VT-102's ``pipeline_log`` JSONB rows do not drift. New pattern-driven
redactions (PAN, Aadhaar, IFSC, GST, CC, long body, email regex) flow
through the canonical module's ``redact`` directly.

Bypass requires replacing the decorator wrapper at the call site (see
``observability/langsmith.py``).
"""

from __future__ import annotations

from typing import Any

from orchestrator.privacy.pii_redactor import redact


def redact_for_langsmith(value: Any, _depth: int = 0) -> Any:
    """Return a PII-safe copy of ``value`` for the LangSmith sink.

    Delegates to :func:`orchestrator.privacy.pii_redactor.redact`. The
    ``_depth`` parameter preserves VT-101's signature for any callers
    that still pass it positionally; mapped to the canonical ``depth``.
    """
    return redact(value, depth=_depth)


# VT-102: alias for the pipeline_log writer. Same redactor; call-site
# clarity at non-LangSmith sinks. Kept as an alias rather than a
# delegating function so the symbol table stays minimal.
redact_for_log = redact_for_langsmith


__all__ = ["redact_for_langsmith", "redact_for_log"]
