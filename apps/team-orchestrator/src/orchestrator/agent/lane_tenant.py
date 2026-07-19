"""VT-599 — shared tenant-resolution helper for the six business-manager LANE tool surfaces.

THE DEFECT this closes (live, deployed dev 3c98a78, VT-598 pack): every lane tool
(marketing / accounting / finance / tech / cost_opt — ``sales_lane`` carries no
``tenant_id``-taking tool at all) declares ``tenant_id: str`` as a MODEL-FILLABLE parameter.
Sonnet-5 filled ``marketing_lane.list_recent_campaigns(tenant_id=...)`` with the tenant's
business NAME, not its UUID — ``UUID()`` raised inside the DB wrapper (``db/base.py``'s
``_uuid``), the exception escaped ``graph.invoke`` (these six lane sub-graphs hold NO VT-484
tool-error middleware of their own — that middleware is wired ONLY on the top-level
``orchestrator_agent`` ``create_agent`` build, not on any lane's), and the run hung at
``status='running'`` for the DBOS reaper. Independent of the crash: the DB wrapper scopes RLS
by the PASSED id — a model-authored foreign UUID is the VT-293/294 IDOR class (server-side
scope derivation is the standing rule; a model-supplied scope is never trusted).

THE FIX: the AUTHORITATIVE tenant is the ambient dispatch ``ObservabilityContext`` — the
ContextVar ``observability_context(...)`` sets around ``graph.invoke`` in ``agent/dispatch.py``.
The lane sub-graphs execute as nodes of that SAME graph invocation, in the SAME logical run
(langchain's runnable executors ``contextvars.copy_context().run(...)`` any thread-pool hop, so
the ContextVar survives even parallel tool dispatch — the same mechanism the existing
``compose_owner_output`` / ``self_evaluate`` ``@tool_step(tenant_from_context=True)`` seam
already relies on, one level up at the manager). ``resolve_lane_tenant`` reads it FIRST and
never trusts a model-supplied value that disagrees (it logs a mismatch, but still returns the
context tenant). Tool SIGNATURES are unchanged (``tenant_id: str`` stays a declared param —
prompts/bindings don't break); only the TRUST changes.

Mirrors the existing ``record_business_objective`` / ``search_conversation_history`` precedent
in ``agent/orchestrator_agent.py`` (same ContextVar-first, honest-error-not-a-raise pattern),
generalised into ONE shared helper so the ~23 affected lane tools call one seam instead of each
duplicating the resolution + fallback logic.

Lazy-imports ``_observability_context`` INSIDE the function (not at module top) — this module
stays import-light (no psycopg / DB chain at import time), matching the lane files' own
lazy-import discipline for heavy dependencies (the dep-less smoke suite must still collect it).
"""

from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger("orchestrator.agent.lane_tenant")


def resolve_lane_tenant(model_value: str | None, *, tool_name: str) -> UUID | None:
    """Resolve the AUTHORITATIVE tenant for a lane tool call — context wins, never the model.

    Resolution order:
      1. The ambient dispatch ``ObservabilityContext`` (set by ``observability_context(...)``
         around the run's ``graph.invoke``) — when present, THIS is authoritative regardless of
         what the model passed. If ``model_value`` is ALSO present and disagrees (a different
         UUID, a business name, garbage), a WARNING is logged naming the tool + a truncated
         (<=20 char) prefix of the model's value — never the full value (log hygiene) — and the
         CONTEXT tenant is returned anyway. The model's value is observed, never trusted.
      2. No ambient context (e.g. a direct unit-test call, or a lane tool somehow invoked outside
         dispatch): fall back to parsing ``model_value`` as a UUID. Returned only if it parses.
      3. Neither resolves -> ``None``. Callers MUST return a structured tool-error dict on
         ``None`` (mirrors the VT-484 tool-error invariant: a lane tool must never RAISE on a bad
         tenant_id — these lane sub-graphs hold no tool-error middleware of their own, so a raise
         here would orphan the tool_use / hang the run, exactly the VT-599 live defect).
    """
    from orchestrator.observability.decorators import _observability_context

    ctx = _observability_context.get()
    if ctx is not None:
        if model_value is not None and not _model_value_matches(model_value, ctx.tenant_id):
            logger.warning(
                "lane tool tenant_id mismatch — model-supplied value ignored, using the run's "
                "context tenant (tool=%s model_value_prefix=%r)",
                tool_name,
                str(model_value)[:20],
            )
        return ctx.tenant_id
    if model_value is not None:
        try:
            return UUID(str(model_value))
        except (ValueError, TypeError, AttributeError):
            return None
    return None


def _model_value_matches(model_value: str, context_tenant: UUID) -> bool:
    """True iff ``model_value`` parses as a UUID AND equals ``context_tenant``."""
    try:
        return UUID(str(model_value)) == context_tenant
    except (ValueError, TypeError, AttributeError):
        return False


def lane_tenant_error(tool_name: str) -> dict[str, str]:
    """The structured tool-error dict a lane tool returns on an unresolvable tenant (no raise).

    Shape mirrors the existing ``record_business_objective`` / ``search_conversation_history``
    precedent in ``agent/orchestrator_agent.py``: ``{"status": "error", "error": "<msg>"}``.
    """
    return {"status": "error", "error": f"{tool_name}: no resolvable tenant context"}


__all__ = ["lane_tenant_error", "resolve_lane_tenant"]
