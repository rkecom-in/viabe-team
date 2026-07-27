"""VT-709 static gates for migrations 182/183 (the migrations are never run by Codex)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.knowledge_global_purity import (
    GlobalKnowledgePurityError,
    assert_global_payload_pure,
)

ROOT = Path(__file__).resolve().parents[5]
MIG_182 = ROOT / "migrations" / "182_vt709_o8_card_registry.sql"
MIG_183 = ROOT / "migrations" / "183_vt709_o8_tenant_evidence.sql"
DSR_PURGE = ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "dsr_purge.py"

GLOBAL_TABLES = (
    "knowledge_sources",
    "knowledge_cards",
    "knowledge_card_sources",
    "knowledge_corpus_versions",
    "knowledge_corpus_members",
    "knowledge_evaluations",
    "knowledge_lifecycle_events",
)
TENANT_TABLES = ("decision_evidence_links", "knowledge_incidents")


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _create_table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE public\.{re.escape(table)}\s*\((?P<body>.*?)\n\);",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert match is not None, f"missing CREATE TABLE for {table}"
    return match.group("body")


def test_allocated_migration_names_and_exact_nine_tables() -> None:
    assert MIG_182.exists() and MIG_183.exists()
    created = re.findall(
        r"CREATE TABLE public\.([a-z_]+)", _sql(MIG_182) + "\n" + _sql(MIG_183), re.IGNORECASE
    )
    assert tuple(created) == GLOBAL_TABLES + TENANT_TABLES


def test_global_tables_structurally_have_no_tenant_id_and_revoke_app_writes() -> None:
    sql = _sql(MIG_182)
    for table in GLOBAL_TABLES:
        assert "tenant_id" not in _create_table_body(sql, table).lower()
    revoke = sql[sql.index("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON") :]
    for table in GLOBAL_TABLES:
        assert f"public.{table}" in revoke
    assert "FROM app_role" in revoke


def test_cards_are_immutable_and_lifecycle_is_append_only() -> None:
    sql = _sql(MIG_182)
    assert "CREATE TRIGGER knowledge_cards_no_update" in sql
    assert "BEFORE UPDATE ON public.knowledge_cards" in sql
    assert "CREATE TRIGGER knowledge_cards_rights_delete_guard" in sql
    assert "event_type = 'rights_removal'" in sql
    assert "CREATE TRIGGER knowledge_lifecycle_events_no_row_mutate" in sql
    assert "BEFORE UPDATE OR DELETE ON public.knowledge_lifecycle_events" in sql
    assert "CREATE TRIGGER knowledge_lifecycle_events_no_truncate" in sql


def test_retention_and_rights_removal_contract_is_present() -> None:
    sql = _sql(MIG_182)
    assert _create_table_body(sql, "knowledge_sources").count("retention_class") >= 1
    assert _create_table_body(sql, "knowledge_cards").count("retention_class") >= 1
    assert "expires_at <= acquired_at + INTERVAL '6 months'" in sql
    assert "'expiry'" in _create_table_body(sql, "knowledge_lifecycle_events")
    assert "'rights_removal'" in _create_table_body(sql, "knowledge_lifecycle_events")
    assert "ON DELETE SET NULL" in _create_table_body(sql, "knowledge_lifecycle_events")


def test_retrieval_indexes_and_every_global_fk_are_indexed() -> None:
    sql = _sql(MIG_182)
    for required in (
        "knowledge_cards_claim_status",
        "knowledge_cards_scope_status",
        "knowledge_cards_validated",
        "knowledge_cards_supersedes_fk",
        "knowledge_card_sources_card_fk",
        "knowledge_card_sources_source_fk",
        "knowledge_corpus_versions_parent_fk",
        "knowledge_corpus_members_corpus_fk",
        "knowledge_corpus_members_card_fk",
        "knowledge_evaluations_corpus_fk",
        "knowledge_evaluations_baseline_corpus_fk",
        "knowledge_evaluations_card_fk",
        "knowledge_lifecycle_events_card_fk",
    ):
        assert f"CREATE INDEX {required}" in sql
    assert "WHERE status = 'validated'" in sql


def test_tenant_tables_force_rls_with_complete_crud_policies() -> None:
    sql = _sql(MIG_183)
    for table in TENANT_TABLES:
        body = _create_table_body(sql, table).lower()
        assert re.search(r"tenant_id\s+uuid\s+not null", body)
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in sql
        for operation in ("select", "insert", "update", "delete"):
            assert f"CREATE POLICY {table}_{operation}" in sql
        assert sql.count("tenant_id = app_current_tenant()") >= 4


def test_dsr_inventory_contains_o8_tables_in_canonical_order() -> None:
    # Parse the constant without importing dsr_purge: its runtime module correctly depends on the
    # DBOS/LangGraph stack, while this migration-inventory gate belongs in the dep-less smoke suite.
    tree = ast.parse(DSR_PURGE.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_PURGE_ORDER"
    )
    assert assignment.value is not None
    purge_order = ast.literal_eval(assignment.value)

    assert purge_order.index("decision_evidence_links") < purge_order.index(
        "knowledge_incidents"
    )


def test_seeded_tenant_identifier_is_rejected_from_global_prior_payload() -> None:
    tenant_id = str(uuid4())
    candidate = {
        "scope": "prior",
        "claim": f"A lesson distilled from tenant {tenant_id}",
        "claim_value": {"value_type": "boolean", "value": True},
    }
    with pytest.raises(GlobalKnowledgePurityError, match="tenant identifier"):
        assert_global_payload_pure(candidate, tenant_identifiers=(tenant_id,))

    assert_global_payload_pure(
        {
            "scope": "prior",
            "claim": "Aggregated evidence supports a cautious operating hypothesis.",
        },
        tenant_identifiers=(tenant_id,),
    )
