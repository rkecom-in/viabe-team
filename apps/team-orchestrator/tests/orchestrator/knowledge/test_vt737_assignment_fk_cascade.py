"""VT-737 — the assignment/card FK cascade must be able to actually fire.

Migration 186 wrote an append-only exemption whose comment says it exists to "preserve event
tombstones when a referenced assignment/card is removed". It could never run. The FK was
`FOREIGN KEY (tenant_id, assignment_id) ... ON DELETE SET NULL`, and a bare SET NULL on a COMPOSITE
FK nulls every column in it — so the cascade emitted `SET tenant_id = NULL, assignment_id = NULL`,
which (a) failed the exemption's "nothing else changed" test and (b) violated `tenant_id NOT NULL`.
Deleting an assignment that had any referencing event raised, always.

Nothing caught it because no test ever deleted an assignment that had a referencing event — the
schema *looked* right and the trigger comment described behaviour the schema could not produce. So
this test reads the migration text: the defect was in the DDL, and the DDL is where it can regress.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[5] / "migrations"


def _sql(stem_prefix: str) -> str:
    matches = sorted(MIGRATIONS.glob(f"{stem_prefix}*.sql"))
    assert matches, f"no migration matching {stem_prefix}*"
    return matches[0].read_text(encoding="utf-8")


def _constraint(sql: str, name: str) -> str:
    """The text of one ADD CONSTRAINT ... ; statement."""

    match = re.search(rf"ADD CONSTRAINT {name}\b(.*?);", sql, re.S)
    assert match, f"{name} is not added in this migration"
    return " ".join(match.group(1).split())


def test_both_cascades_null_only_their_own_column_not_the_whole_composite_key() -> None:
    sql = _sql("198_")
    for name, nulled in (
        ("specialist_memory_events_assignment_tenant_fk", "assignment_id"),
        ("specialist_memory_events_card_tenant_fk", "memory_card_id"),
    ):
        body = _constraint(sql, name)
        set_null = re.search(r"ON DELETE SET NULL\s*(\([^)]*\))?", body)
        assert set_null, f"{name} must still cascade with SET NULL"
        columns = set_null.group(1)
        assert columns == f"({nulled})", (
            f"{name} nulls {columns or 'the ENTIRE composite key (bare SET NULL)'}; it must null "
            f"exactly ({nulled}). A bare SET NULL on a composite FK also nulls tenant_id, which is "
            f"NOT NULL and additionally defeats the append-only tombstone exemption — the cascade "
            f"then cannot fire at all, which is the VT-737 defect."
        )


def test_the_durable_audit_reference_is_a_separate_column_the_cascade_never_touches() -> None:
    """`assignment_id` is the live FK and is cleared; `assignment_ref` is the permanent record.

    If a future change ever pointed the FK at the _ref columns, nulling them would erase the audit
    trail rather than tombstone it, and `specialist_memory_events_exactly_one_target` would start
    failing on delete instead.
    """

    sql = _sql("198_")
    for name in (
        "specialist_memory_events_assignment_tenant_fk",
        "specialist_memory_events_card_tenant_fk",
    ):
        body = _constraint(sql, name)
        assert "assignment_ref" not in body and "memory_card_ref" not in body


def test_migration_186_still_declares_the_tombstone_exemption_this_fix_enables() -> None:
    """The fix is only correct while the exemption it unblocks still exists."""

    sql = _sql("186_")
    assert "Preserve event tombstones" in sql
    assert "pg_trigger_depth() > 1" in sql
    assert "specialist_memory_events is append-only (VT-711)" in sql
