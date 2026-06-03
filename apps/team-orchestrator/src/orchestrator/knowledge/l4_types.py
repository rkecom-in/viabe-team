"""VT-70 — L4 skill-corpus types."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class L4Document(BaseModel):
    """A retrieved L4 corpus document (read model). ``score`` is the cosine
    similarity × priority ranking value from retrieval (None when not ranked)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    body: str
    tags: list[str]
    applies_to_business_types: list[str] | None
    applies_to_city_tiers: list[str] | None
    priority: int
    authored_by: str
    score: float | None = None


__all__ = ["L4Document"]
