"""Static fail-closed gates for the written-not-run VT-726 migration 189."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "migrations/189_vt726_card_retrieval_projection.sql"


def test_migration_adds_complete_single_table_knowledge_card_projection() -> None:
    sql = MIGRATION.read_text()
    for column in (
        "domain",
        "source_class",
        "usage_rights",
        "independence_cluster",
        "corroboration_cluster_count",
        "provenance",
        "retrieval_eligible",
        "corpus_version_id",
    ):
        assert f"ADD COLUMN {column}" in sql
    assert "CREATE INDEX knowledge_cards_domain_status" in sql
    assert "ON public.knowledge_cards (domain, status)" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "pg_trigger_depth() > 1" in sql
    assert "OLD.supersedes_card_id IS NOT NULL" in sql
    assert "NEW.supersedes_card_id IS NULL" in sql
    assert "to_jsonb(NEW) - 'supersedes_card_id'" in sql


def test_migration_preserves_global_boundary_and_does_not_activate_o8() -> None:
    sql = MIGRATION.read_text()
    executable = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "tenant_id" not in executable
    assert "GRANT " not in executable
    assert "UPDATE public.knowledge_cards" not in executable
    assert "INSERT INTO public.knowledge" not in executable
    assert "admission_verdict = 'passed'" not in executable
    assert "retrieval_eligible BOOLEAN NOT NULL DEFAULT false" in sql
