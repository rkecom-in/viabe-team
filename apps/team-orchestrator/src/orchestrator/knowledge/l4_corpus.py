"""VT-70 — L4 skill-corpus seed loader (importable).

Reads hand-authored markdown docs (YAML frontmatter + body) from a seed dir,
embeds each body with voyage-4-lite ('document' input_type), and UPSERTs into
``l4_documents`` (idempotent on title+version). The REAL ≥30-doc corpus is a
Fazal/Clau authoring deliverable (VT-313); this loader is the pipeline. The
``scripts/l4_seed.py`` CLI is a thin wrapper around ``seed_l4_corpus``.

Frontmatter keys: title (req), authored_by (req), tags, applies_to_business_types,
applies_to_city_tiers, priority (1-5, default 3), version (default 1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from orchestrator.graph import get_pool
from orchestrator.knowledge.embeddings import embed_texts, to_pgvector_literal


def parse_doc(path: Path) -> dict[str, Any]:
    """Parse a frontmatter+markdown doc. Raises on missing required keys
    (fail-loud — a malformed corpus doc must not silently seed empty)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    if not meta.get("title") or not meta.get("authored_by"):
        raise ValueError(f"{path.name}: frontmatter needs title + authored_by")
    body = body.strip()
    if not body:
        raise ValueError(f"{path.name}: empty body")
    return {
        "title": str(meta["title"]),
        "authored_by": str(meta["authored_by"]),
        "tags": list(meta.get("tags") or []),
        "applies_to_business_types": meta.get("applies_to_business_types"),
        "applies_to_city_tiers": meta.get("applies_to_city_tiers"),
        "priority": int(meta.get("priority", 3)),
        "version": int(meta.get("version", 1)),
        "body": body,
    }


def seed_l4_corpus(seed_dir: str | Path) -> dict[str, int]:
    """Load every ``*.md`` under ``seed_dir`` into ``l4_documents`` (embedded,
    UPSERT on title+version). Returns {'seeded': n}."""
    docs = [parse_doc(p) for p in sorted(Path(seed_dir).glob("*.md"))]
    if not docs:
        return {"seeded": 0}
    # Batch-embed the bodies as 'document' (asymmetric retrieval vs the query).
    vectors = embed_texts([d["body"] for d in docs], input_type="document")
    with get_pool().connection() as conn, conn.transaction():
        for doc, vec in zip(docs, vectors, strict=True):
            conn.execute(
                """
                INSERT INTO l4_documents
                  (title, body, body_embedding, tags, applies_to_business_types,
                   applies_to_city_tiers, priority, authored_by, version)
                VALUES (%s, %s, %s::vector, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (title, version) DO UPDATE SET
                  body = EXCLUDED.body,
                  body_embedding = EXCLUDED.body_embedding,
                  tags = EXCLUDED.tags,
                  applies_to_business_types = EXCLUDED.applies_to_business_types,
                  applies_to_city_tiers = EXCLUDED.applies_to_city_tiers,
                  priority = EXCLUDED.priority,
                  authored_by = EXCLUDED.authored_by,
                  updated_at = now()
                """,
                (
                    doc["title"], doc["body"], to_pgvector_literal(vec),
                    doc["tags"], doc["applies_to_business_types"],
                    doc["applies_to_city_tiers"], doc["priority"],
                    doc["authored_by"], doc["version"],
                ),
            )
    return {"seeded": len(docs)}


__all__ = ["parse_doc", "seed_l4_corpus"]
