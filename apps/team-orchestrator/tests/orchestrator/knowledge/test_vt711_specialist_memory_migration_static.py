"""Static fail-closed gates for VT-711 migration 186 and its DSR inventory."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "migrations" / "186_vt711_specialist_memory_and_assignment.sql"
DSR_PURGE = ROOT / "apps" / "team-orchestrator" / "src" / "orchestrator" / "dsr_purge.py"
TENANT_TABLES = (
    "knowledge_card_assignments",
    "specialist_memory_cards",
    "specialist_memory_events",
)


def _table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE public\.{re.escape(table)}\s*\((?P<body>.*?)\n\);",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert match is not None, f"missing CREATE TABLE for {table}"
    return match.group("body")


def _purge_order() -> tuple[str, ...]:
    tree = ast.parse(DSR_PURGE.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_PURGE_ORDER"
    )
    assert assignment.value is not None
    return ast.literal_eval(assignment.value)


def test_migration_186_owns_exactly_three_tenant_tables_and_no_global_lifecycle_change() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert MIGRATION.name == "186_vt711_specialist_memory_and_assignment.sql"
    assert tuple(re.findall(r"CREATE TABLE public\.([a-z_]+)", sql)) == TENANT_TABLES
    assert "ADD COLUMN default_assignment" in sql
    assert "^specialist:[a-z][a-z0-9_]{0,99}$" in sql
    assert "ALTER TABLE public.knowledge_lifecycle_events" not in sql


def test_all_tenant_tables_force_rls_and_have_complete_policies() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in TENANT_TABLES:
        assert re.search(r"tenant_id\s+UUID\s+NOT NULL", _table_body(sql, table))
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in sql
        for operation in ("select", "insert", "update", "delete"):
            assert f"CREATE POLICY {table}_{operation}" in sql


def test_specialist_memory_is_structurally_thin_attributable_and_versioned() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    cards = _table_body(sql, "specialist_memory_cards")
    events = _table_body(sql, "specialist_memory_events")
    assert "memory_type = 'task_customization'" in cards
    assert "'specialist:' || agent" in cards
    assert "authored_by IN ('vtr', 'manager')" in cards
    assert "task_scope" in cards and "version" in cards and "supersedes_memory_card_id" in cards
    assert "actor IN ('vtr', 'manager')" in events
    assert "CREATE TRIGGER specialist_memory_cards_emit_event" in sql
    assert "CREATE TRIGGER knowledge_card_assignments_emit_event" in sql
    assert "CREATE TRIGGER specialist_memory_events_no_row_mutate" in sql
    assert "CREATE TRIGGER specialist_memory_events_no_truncate" in sql
    assert "pg_trigger_depth() > 1" in sql
    assert "current_setting('app.dsr_purge', true) = 'on'" in sql


def test_dsr_inventory_is_events_then_memory_then_assignments_and_sets_marker() -> None:
    order = _purge_order()
    indexes = tuple(order.index(table) for table in reversed(TENANT_TABLES))
    assert indexes == tuple(sorted(indexes))
    source = DSR_PURGE.read_text(encoding="utf-8")
    marker = "SELECT set_config('app.dsr_purge', 'on', true)"
    assert marker in source
    assert source.index(marker) < source.index("for table in _PURGE_ORDER")
