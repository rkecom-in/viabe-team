"""Knowledge layers — L1 KG today; L2 / L3 (Mem0-backed) post-launch (CL-324)."""

from orchestrator.knowledge.l1 import (
    L1Entity,
    L1Path,
    L1Relationship,
    MAX_TRAVERSAL_DEPTH,
    search_entities,
    traverse_relationships,
)

__all__ = [
    "L1Entity",
    "L1Path",
    "L1Relationship",
    "MAX_TRAVERSAL_DEPTH",
    "search_entities",
    "traverse_relationships",
]
