"""Shared types for the observability surface (VT-102).

``PipelineLogEvent`` is the read-side dataclass returned by every function in
``query.py``. Lives in a sibling module so ``log.py`` and ``query.py`` can
both import without forming a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class PipelineLogEvent:
    """One row of ``pipeline_log`` materialised for Python consumers.

    ``tenant_id`` is ``None`` for workspace-level events. ``duration_ms`` is
    ``None`` when the originating call site doesn't measure duration (most
    non-RPC events).
    """

    id: UUID
    run_id: UUID
    tenant_id: UUID | None
    event_type: str
    severity: str
    component: str
    payload: dict[str, Any]
    duration_ms: int | None
    created_at: datetime


__all__ = ["PipelineLogEvent"]
