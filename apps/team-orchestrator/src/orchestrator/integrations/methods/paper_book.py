"""VT-55 / VT-6 Method 1 — paper-book photograph ingestion.

Thin wrapper over the shared image adapter (Pillar 8): the owner photographs
their handwritten customer book; this extracts entries, routes low-confidence
ones to the clarifying flow, dedups + commits identity, and persists transactions.

acquired_via="paper_book". Image BYTES in (the Twilio media-URL fetch is the
webhook layer's job — VT-3.3 — not this primitive's). Returns counts only (no PII).
"""

from __future__ import annotations

from uuid import UUID

from orchestrator.integrations.methods._image_adapter import (
    IngestionSummary,
    ingest_image,
)


def ingest_paper_book(
    tenant_id: UUID | str, image_bytes: bytes, *, media_type: str = "image/jpeg", **kwargs
) -> IngestionSummary:
    """Ingest a paper-book photo. tenant_id from invocation context (P3)."""
    return ingest_image(
        tenant_id, image_bytes, acquired_via="paper_book",
        media_type=media_type, **kwargs,
    )


__all__ = ["ingest_paper_book"]
