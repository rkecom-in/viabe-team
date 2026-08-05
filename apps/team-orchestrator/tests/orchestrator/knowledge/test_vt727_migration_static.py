"""Static privacy/ownership contract for allocated migration 194 (written, not run by Codex)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "migrations" / "194_vt727_o8_full_corpus_load.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _table_body(sql: str) -> str:
    match = re.search(
        r"CREATE TABLE public\.knowledge_card_embeddings\s*\((.*?)\n\);",
        sql,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_allocated_migration_is_single_schema_boundary_and_195_stays_unused() -> None:
    assert MIGRATION.is_file()
    assert not list((ROOT / "migrations").glob("195_*.sql"))
    sql = _sql()
    assert "ALLOCATION: 194 was allocated by Clau for VT-727" in sql
    assert "Migration 195 is intentionally unused" in sql
    assert "WRITTEN, NOT RUN by Codex" in sql
    # Corpus rows remain pipeline-derived; the migration must not smuggle authored card content.
    assert "INSERT INTO public.knowledge_cards" not in sql


def test_embedding_table_is_global_tenant_free_and_card_lifecycle_bound() -> None:
    sql = _sql()
    body = _table_body(sql)
    assert "tenant_id" not in body
    assert "card_id" in body
    assert "REFERENCES public.knowledge_cards (id) ON DELETE CASCADE" in body
    assert "embedding_model = 'voyage-4-lite'" in body
    assert "embedding_dimensions = 1024" in body
    assert "vector(1024) NOT NULL" in body
    assert "content_digest ~ '^[0-9a-f]{64}$'" in body
    assert "raw_text" not in body


def test_embedding_table_is_service_write_app_read_and_ann_ready() -> None:
    sql = _sql()
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert re.search(
        r"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER\s+"
        r"ON public\.knowledge_card_embeddings FROM app_role;",
        sql,
    )
    assert "GRANT SELECT ON public.knowledge_card_embeddings TO app_role;" in sql
    assert "REVOKE ALL ON public.knowledge_card_embeddings FROM PUBLIC;" in sql
    assert "advisory only" in sql
