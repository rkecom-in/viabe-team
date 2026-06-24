"""PII redaction at observability sinks (VT-101 → VT-104 → VT-171).

Thin delegation layer over the canonical
:mod:`orchestrator.privacy.pii_redactor`. The redactor seam is preserved
byte-identical across the LangSmith → Logfire migration; only the public
export NAME changes (``redact_for_langsmith`` → ``redact_for_otel_span``)
to remove vendor coupling from the surface.

Public exports
--------------
- :func:`redact_for_otel_span` — the canonical vendor-neutral redactor
  call. Use this from new code.
- :func:`redact_for_log` — alias preserved for the ``pipeline_log``
  writer (no name change at that sink).
- :func:`redact_for_langsmith` — **DEPRECATED**; emits
  :class:`DeprecationWarning` and delegates. Removed in VT-172.
"""

from __future__ import annotations

import warnings
from typing import Any, Callable

from orchestrator.privacy.pii_redactor import redact


def redact_for_otel_span(
    value: Any,
    _depth: int = 0,
    *,
    name_registry: Callable[[str], bool] | None = None,
    registry_down: bool = False,
) -> Any:
    """Return a PII-safe copy of ``value`` for an OTel-style span sink.

    Delegates to :func:`orchestrator.privacy.pii_redactor.redact`. The
    ``_depth`` parameter preserves VT-101's call signature for callers
    that still pass it positionally; mapped to the canonical ``depth``.

    VT-170: ``name_registry`` (default ``None`` — None-safe, no behaviour
    change for the many call-sites without tenant context) lets a
    tenant-scoped caller inject the customer-name registry callable from
    :func:`orchestrator.privacy.customer_registry.make_name_registry` so
    known customer names get redacted by exact match.

    VT-386: ``registry_down=True`` (the fail-soft-split outage signal) makes the
    redactor strip the known name-keys to ``<name:registry_down>`` without a
    registry. Default ``False`` — no behaviour change for existing callers.
    """
    return redact(
        value,
        depth=_depth,
        name_registry=name_registry,
        registry_down=registry_down,
    )


# pipeline_log writer — same redactor; call-site clarity at non-Logfire
# sinks. Alias rather than a separate function so the symbol table stays
# minimal.
redact_for_log = redact_for_otel_span


def redact_for_langsmith(value: Any, _depth: int = 0) -> Any:
    """DEPRECATED — use :func:`redact_for_otel_span` instead.

    Kept as a one-cycle alias so any straggler import keeps working after
    the VT-171 LangSmith → Logfire migration. Emits
    :class:`DeprecationWarning` on every call. Removed in VT-172.
    """
    warnings.warn(
        "redact_for_langsmith is deprecated (VT-171 / CL-56 hot-fix); "
        "use redact_for_otel_span instead. Removed in VT-172.",
        DeprecationWarning,
        stacklevel=2,
    )
    return redact_for_otel_span(value, _depth)


__all__ = ["redact_for_langsmith", "redact_for_log", "redact_for_otel_span"]
