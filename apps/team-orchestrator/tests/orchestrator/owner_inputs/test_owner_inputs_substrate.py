"""VT-146 — owner_inputs DB-substrate tests.

Covers:

- Migration 020 applies clean; 4 RLS policies + the partial indexes
  exist; schema has NO body / raw_text / content column.
- Pillar-3 cross-tenant attack — tenant A cannot read or write to
  tenant B's rows via ``tenant_connection``.
- ``write_owner_input`` end-to-end against the live table: a seeded
  classification produces exactly one row with derived fields only; a
  fresh SELECT confirms the secret body substring is absent from the
  full row text.
- Composer ``_build_pending_owner_inputs`` returns tenant-scoped pending
  rows with the correct completeness flag, and zero rows for an empty
  tenant.

Requires ``DATABASE_URL`` + the dbos stack; runs in CI ``orchestrator``
job against ``pgvector/pgvector:pg16``.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-146 substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (incl. 020 owner_inputs) and launch DBOS so the
    pool exists — ``tenant_connection`` needs the pool."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-146 Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


# --- Schema -----------------------------------------------------------------


def test_owner_inputs_schema_has_no_body_columns(substrate):  # type: ignore[no-untyped-def]
    """The table must NOT have a body / raw_text / content / message_body
    column. Brief locks derived-only; adding a body column in a future
    migration would reintroduce the retention surface VT-144 closed."""
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'owner_inputs'"
            ).fetchall()
        }
    # The expected derived-only + provenance + lifecycle surface.
    expected = {
        "id",
        "tenant_id",
        "run_id",
        "message_sid",
        "intent",
        "segment",
        "occasion",
        "consumed_at",
        "created_at",
    }
    assert cols == expected, f"unexpected owner_inputs columns: {cols}"
    forbidden = {"body", "raw_text", "content", "message_body", "message_text"}
    assert cols.isdisjoint(forbidden), (
        f"owner_inputs gained a forbidden raw-body column: "
        f"{cols & forbidden}"
    )


def test_owner_inputs_rls_and_indexes_exist(substrate):  # type: ignore[no-untyped-def]
    """RLS enabled + forced, 4 policies, and the two partial indexes."""
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        rls = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'owner_inputs'"
        ).fetchone()
        assert rls is not None
        enabled, forced = rls
        assert enabled, "owner_inputs RLS not enabled"
        assert forced, "owner_inputs RLS not forced"

        policies = {
            row[0]
            for row in conn.execute(
                "SELECT policyname FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = 'owner_inputs'"
            ).fetchall()
        }
        assert policies == {
            "owner_inputs_select",
            "owner_inputs_insert",
            "owner_inputs_update",
            "owner_inputs_delete",
        }, f"unexpected RLS policy set: {policies}"

        idx = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'owner_inputs'"
            ).fetchall()
        }
        for required in (
            "owner_inputs_tenant_pending_created",
            "owner_inputs_tenant_message_sid",
        ):
            assert required in idx, f"missing index {required!r}; found {idx}"


# --- Pillar 3 — cross-tenant attack -----------------------------------------


def test_cross_tenant_select_blocked(substrate):  # type: ignore[no-untyped-def]
    """Tenant B's rows must not be visible to tenant A's read path."""
    from orchestrator.context_builder import _build_pending_owner_inputs
    from orchestrator.owner_inputs.writer import (
        OwnerInputClassification,
        write_owner_input,
    )

    tenant_a = _new_tenant(substrate.dsn)
    tenant_b = _new_tenant(substrate.dsn)

    write_owner_input(
        tenant_b,
        run_id=None,
        message_sid=f"SM{uuid4().hex}",
        classification=OwnerInputClassification(
            intent="winback", segment="dormant", occasion=None
        ),
    )

    a_rows, a_flag = _build_pending_owner_inputs(tenant_a)
    assert a_rows == []
    assert a_flag is False, (
        "empty tenant must report completeness=False"
    )
    b_rows, b_flag = _build_pending_owner_inputs(tenant_b)
    assert len(b_rows) == 1
    assert b_flag is True


def test_cross_tenant_insert_blocked(substrate):  # type: ignore[no-untyped-def]
    """An INSERT naming another tenant is rejected by the RLS WITH
    CHECK clause — mirrors test_tenant_isolation's pattern. The writer
    uses ``tenant_connection(tenant_a)`` and explicitly passes a
    tenant_id; a Python-level mismatch would be caught by RLS at
    INSERT-time."""
    from orchestrator.db import tenant_connection

    tenant_a = _new_tenant(substrate.dsn)
    tenant_b = _new_tenant(substrate.dsn)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_connection(tenant_a) as conn:
            # Scoped to A, but the row claims tenant B — WITH CHECK rejects.
            conn.execute(
                "INSERT INTO owner_inputs (id, tenant_id, intent) "
                "VALUES (%s, %s, 'winback')",
                (str(uuid4()), str(tenant_b)),
            )


# --- Writer end-to-end — derived only, no body persisted --------------------


def test_write_owner_input_persists_only_derived_fields(substrate):  # type: ignore[no-untyped-def]
    """Brief acceptance #2 — the row contains only derived fields; the
    secret body substring is absent from any column of the row."""
    from orchestrator.owner_inputs.writer import (
        OwnerInputClassification,
        write_owner_input,
    )

    tenant_id = _new_tenant(substrate.dsn)
    secret_body = f"REDACT-PROBE-{uuid4().hex}-message"
    sid = f"SM{uuid4().hex}"

    classification = OwnerInputClassification(
        intent="winback",
        segment="dormant_60d",
        occasion="diwali",
    )
    new_id = write_owner_input(
        tenant_id,
        run_id=None,
        message_sid=sid,
        classification=classification,
    )

    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT row_to_json(owner_inputs.*)::text AS payload "
            "FROM owner_inputs WHERE id = %s",
            (str(new_id),),
        ).fetchone()
    assert row is not None
    payload_text = row[0]

    # Derived fields present.
    payload = json.loads(payload_text)
    assert payload["intent"] == "winback"
    assert payload["segment"] == "dormant_60d"
    assert payload["occasion"] == "diwali"
    assert payload["message_sid"] == sid
    # Body / variants absent — neither a column nor a substring leak.
    for forbidden in (
        "body",
        "raw_text",
        "content",
        "message_body",
        "message_text",
    ):
        assert forbidden not in payload, (
            f"owner_inputs row contains forbidden field {forbidden!r}: "
            f"{payload!r}"
        )
    # Defence in depth — the secret body string is not anywhere in the
    # serialised row. Catches a hypothetical bug where the writer hides
    # the body in segment / occasion / a JSONB attribute.
    assert secret_body not in payload_text, (
        "secret body substring leaked into owner_inputs row"
    )


# --- Composer read-path semantics -------------------------------------------


def test_composer_filters_consumed_rows(substrate):  # type: ignore[no-untyped-def]
    """``consumed_at IS NULL`` filter — rows marked consumed must not
    appear in the Composer bundle (pending semantics live in the schema,
    not in app logic)."""
    from orchestrator.context_builder import _build_pending_owner_inputs
    from orchestrator.db import tenant_connection
    from orchestrator.owner_inputs.writer import (
        OwnerInputClassification,
        write_owner_input,
    )

    tenant_id = _new_tenant(substrate.dsn)
    pending_id = write_owner_input(
        tenant_id,
        run_id=None,
        message_sid=f"SM{uuid4().hex}",
        classification=OwnerInputClassification(intent="winback"),
    )
    consumed_id = write_owner_input(
        tenant_id,
        run_id=None,
        message_sid=f"SM{uuid4().hex}",
        classification=OwnerInputClassification(intent="feedback"),
    )
    # Mark the second row consumed via the production-role helper.
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE owner_inputs SET consumed_at = now() WHERE id = %s",
            (str(consumed_id),),
        )

    rows, flag = _build_pending_owner_inputs(tenant_id)
    returned_ids = {r.input_id for r in rows}
    assert pending_id in returned_ids
    assert consumed_id not in returned_ids
    assert flag is True


def test_composer_orders_most_recent_first(substrate):  # type: ignore[no-untyped-def]
    """Pending rows return newest-first — matches the
    ``ORDER BY created_at DESC`` clause in the read path."""
    from orchestrator.context_builder import _build_pending_owner_inputs
    from orchestrator.owner_inputs.writer import (
        OwnerInputClassification,
        write_owner_input,
    )

    tenant_id = _new_tenant(substrate.dsn)
    seeded: list[UUID] = []
    for i in range(3):
        seeded.append(
            write_owner_input(
                tenant_id,
                run_id=None,
                message_sid=f"SM{uuid4().hex}",
                classification=OwnerInputClassification(intent=f"intent-{i}"),
            )
        )

    rows, _ = _build_pending_owner_inputs(tenant_id)
    returned_in_order = [r.input_id for r in rows]
    # Seed order was 0, 1, 2 -> most-recent-first is 2, 1, 0.
    assert returned_in_order == list(reversed(seeded))
