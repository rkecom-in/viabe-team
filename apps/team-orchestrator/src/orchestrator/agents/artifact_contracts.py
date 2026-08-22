"""Shared, effect-free contracts for owner-reviewable draft artifacts.

Content drafts and campaign proposals share lineage and storage shape.  This module does not write
the store; proposer agents return ``UnpersistedArtifact`` values and a separately governed platform
boundary may later persist them.  Keeping recipient and delivery fields structurally absent prevents
this estate from becoming a second outbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Mapping


class ArtifactKind(StrEnum):
    CONTENT_DRAFT = "content_draft"
    CAMPAIGN_PROPOSAL = "campaign_proposal"


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    parent_artifact_id: str | None = None
    parent_version: int | None = None

    def __post_init__(self) -> None:
        if (self.parent_artifact_id is None) != (self.parent_version is None):
            raise ValueError("artifact parent id and version must be supplied together")
        if self.parent_version is not None and self.parent_version < 1:
            raise ValueError("artifact parent version must be positive")


@dataclass(frozen=True, slots=True)
class UnpersistedArtifact:
    """An artifact returned by a proposer.  No instance can claim persistence or effect authority."""

    STORED: ClassVar[bool] = False
    AUTHORIZES_EFFECTS: ClassVar[bool] = False

    artifact_id: str
    kind: ArtifactKind
    version: int
    created_at: datetime
    payload: Mapping[str, object]
    lineage: ArtifactLineage = ArtifactLineage()

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("artifact_id is required")
        if self.version < 1:
            raise ValueError("artifact version must be positive")
        if self.created_at.tzinfo is None:
            raise ValueError("artifact created_at must be timezone-aware")
        forbidden = {"recipient", "recipient_id", "phone", "email", "delivery_state"}
        overlap = forbidden & {str(key).lower() for key in self.payload}
        if overlap:
            raise ValueError(f"draft artifact payload contains send-adjacent fields: {sorted(overlap)}")

    def as_proposal(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.kind.value,
            "artifact_version": self.version,
            "created_at": self.created_at.isoformat(),
            "payload": dict(self.payload),
            "lineage": {
                "parent_artifact_id": self.lineage.parent_artifact_id,
                "parent_version": self.lineage.parent_version,
            },
            "stored": type(self).STORED,
            "effect_authorized": type(self).AUTHORIZES_EFFECTS,
        }


__all__ = ["ArtifactKind", "ArtifactLineage", "UnpersistedArtifact"]
