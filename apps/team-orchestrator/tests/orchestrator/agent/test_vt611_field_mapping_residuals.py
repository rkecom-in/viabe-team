"""VT-611 pre-work #3 — the three VT-608 CRITICAL-2 field-mapping coverage residuals.

(a) A single write -> durable-column -> read round-trip test for the confirmed field_mapping.
    ``test_integration_agent_tools_realdb.py`` already proves confirm_mapping/commit_ingestion
    WRITE the ephemeral + durable halves; other coverage proves ``sheet_row_to_canonical``'s READ
    side against a hand-typed mapping dict. Neither proves the JOIN — that the exact value a real
    confirm+commit persists into ``tenant_connector_status.field_mapping`` is what a later
    recurring pull (``ingest_one_connector`` -> ``_ingest_pulled_rows``) actually reads back and
    applies to land a real customer row.
(b) The partial-mapping sharp edge: a confirmed mapping covering SOME identity fields but not
    others must fall back to the alias table for the fields it doesn't cover — a confirmed mapping
    can only ADD coverage over the alias-only baseline, never silently subtract from it.
(c) Server-side validation that a confirmed mapping's canonical VALUES are real ``CanonicalField``
    literals — an invalid value used to silently no-op that column at read time with no error
    anywhere; ``confirm_mapping`` now rejects the whole call instead of persisting a mapping we
    already know part of can never work.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-611 field-mapping residual tests skipped",
)


@pytest.fixture(scope="module")
def substrate():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt611-test-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _seed_tenant(dsn: str) -> str:
    tid = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt611-{tid[:8]}"),
        )
    return tid


def _ctx(run_id, tenant_id):  # type: ignore[no-untyped-def]
    from orchestrator.observability.decorators import observability_context

    return observability_context(run_id=run_id, tenant_id=tenant_id)


def _confirm_and_commit(dsn: str, tid: str, connector_id: str, mapping: dict) -> dict:  # type: ignore[type-arg]
    """Drive the REAL confirm_mapping + commit_ingestion tools (mirrors
    test_integration_agent_tools_realdb.py's own pattern) so the durable column is written
    through production code, never a hand-typed INSERT."""
    import json

    from orchestrator.agent.integration_agent import commit_ingestion, confirm_mapping

    with _ctx(uuid4(), tid):
        confirmed = confirm_mapping.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id=connector_id, mapping=mapping,
        )
    if not confirmed.get("confirmed"):
        return confirmed
    # confirm_mapping alone carries no spreadsheet_id — seed one directly (mirrors what the
    # picker's own POST /select would already have written before confirm_mapping is called).
    # NOTE: jsonb `||` is a SHALLOW top-level merge — "metadata" must be re-specified whole
    # (including confirmed_mapping) or this UPDATE silently WIPES what confirm_mapping just wrote
    # (mirrors test_integration_agent_tools_realdb.py's own commit_ingestion test's exact idiom).
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE tenant_integration_state SET pending_owner_input = "
            "pending_owner_input || %s::jsonb WHERE tenant_id = %s",
            (
                json.dumps({
                    "metadata": {
                        "spreadsheet_id": "sheet-x", "tab_name": "Sheet1",
                        "confirmed_mapping": mapping,
                    }
                }),
                tid,
            ),
        )
    with _ctx(uuid4(), tid):
        committed = commit_ingestion.func(tenant_id=tid, connector_id=connector_id)  # type: ignore[attr-defined]
    assert committed["status"] == "proposal_recorded", committed
    return confirmed


# ---------------------------------------------------------------------------
# (a) write -> durable-column -> read round trip
# ---------------------------------------------------------------------------


def test_confirmed_mapping_round_trips_write_durable_column_read_and_lands_a_customer(substrate):  # type: ignore[no-untyped-def]
    dsn = substrate
    tid = _seed_tenant(dsn)
    mapping = {"Mobile": "phone", "Email Address": "email", "Full Name": "customer_name"}
    _confirm_and_commit(dsn, tid, "google_sheet", mapping)

    # The durable column — read back, not assumed identical to what was sent.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT field_mapping FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = 'google_sheet'",
            (tid,),
        ).fetchone()
    assert row is not None, "commit_ingestion must persist field_mapping (VT-608 fix round CRITICAL 2)"
    persisted_mapping = row[0]
    assert persisted_mapping == mapping

    # THE JOIN: feed the value ACTUALLY READ BACK from the durable column into the exact function
    # a recurring pull uses (ingest_one_connector -> _ingest_pulled_rows), not a hand-typed dict.
    from orchestrator.integrations.scheduler import _ingest_pulled_rows

    pulled = [
        {"Mobile": "9876543210", "Email Address": "ravi@example.com", "Full Name": "Ravi Kumar"}
    ]
    committed = _ingest_pulled_rows(
        UUID(tid), "google_sheet", pulled, field_mapping=persisted_mapping
    )
    assert committed == 1

    with psycopg.connect(dsn, autocommit=True) as conn:
        cust = conn.execute(
            "SELECT display_name, phone_e164 FROM customers WHERE tenant_id = %s", (tid,)
        ).fetchone()
    assert cust is not None, "the confirmed mapping, round-tripped through the real column, must land a customer"
    assert cust[0] == "Ravi Kumar"
    assert cust[1] == "+919876543210"


# ---------------------------------------------------------------------------
# (b) partial-mapping sharp edge — alias fallback for unmapped identity fields
# ---------------------------------------------------------------------------


def test_partial_mapping_falls_back_to_alias_for_unmapped_identity_fields(substrate):  # type: ignore[no-untyped-def]
    """A mapping confirming ONLY 'phone' must not suppress alias-table detection for email/name —
    the row carries aliased 'email'/'name' columns the mapping never mentions."""
    from orchestrator.integrations.ingest import sheet_row_to_canonical

    row = {"Mobile": "9876543210", "email": "priya@example.com", "name": "Priya Sharma"}
    result = sheet_row_to_canonical(row, mapping={"Mobile": "phone"})

    assert result is not None
    assert result.phone_e164 == "+919876543210"
    # Never in the mapping — the OLD behavior silently dropped these; alias-fallback catches them.
    assert result.email == "priya@example.com"
    assert result.display_name == "Priya Sharma"


def test_partial_mapping_never_fabricates_a_sale_from_an_unmapped_alias_column(substrate):  # type: ignore[no-untyped-def]
    """VT-611 fix round (correcting the earlier ruling this test enshrined): the alias fallback
    applies ONLY to the three IDENTITY fields, NEVER to order_amount/order_date. A mapping
    confirming only 'phone' and staying silent on amount/date is a DELIBERATE "no orders here"
    signal — an aliased 'amount'/'date' column (which might be a store-credit balance or a signup
    date, not a sale) must NOT be read into a fabricated SaleLine. Identity alias-fallback (email)
    still works alongside this."""
    from orchestrator.integrations.ingest import sheet_row_to_canonical

    row = {"Mobile": "9876543210", "email": "priya@example.com", "amount": "499", "date": "2026-01-15"}
    result = sheet_row_to_canonical(row, mapping={"Mobile": "phone"})

    assert result is not None
    assert result.phone_e164 == "+919876543210"
    assert result.email == "priya@example.com"  # identity alias-fallback still works
    assert result.sales == (), "an unmapped amount/date column must never fabricate a sale"


def test_mapping_covers_a_field_its_value_wins_over_a_conflicting_alias(substrate):  # type: ignore[no-untyped-def]
    """The mapping stays AUTHORITATIVE for what it covers (unchanged VT-608 behavior) — a column
    named 'mob' the owner confirmed means 'phone' wins even though an alias-shaped 'phone' column
    ALSO exists in the same row (a genuinely ambiguous sheet)."""
    from orchestrator.integrations.ingest import sheet_row_to_canonical

    row = {"mob": "9123456789", "phone": "9000000001"}
    result = sheet_row_to_canonical(row, mapping={"mob": "phone"})
    assert result is not None
    assert result.phone_e164 == "+919123456789"


def test_mapping_consumed_column_excluded_from_other_fields_alias_scan(substrate):  # type: ignore[no-untyped-def]
    """VT-611 fix round — a column the mapping already claims for ONE canonical field must not be
    cross-read by a DIFFERENT field's alias fallback. A mapping of {"amount_column": "phone"}
    means amount_column IS the phone source — order_amount's alias scan (if it ran one; it
    doesn't, per the sale-field ruling above) must never also read amount_column as a rupee
    figure. This exercises the ``consumed`` exclusion directly via a case where it WOULD matter:
    email's alias fallback must not treat a mapping-consumed column as its own match even if that
    column's name happens to alias-match 'email'."""
    from orchestrator.integrations.ingest import sheet_row_to_canonical

    # "mail" aliases to email — but the mapping already claims that exact column for 'phone'.
    row = {"mail": "9123456789", "name": "Asha Rao"}
    result = sheet_row_to_canonical(row, mapping={"mail": "phone"})

    assert result is not None
    assert result.phone_e164 == "+919123456789"  # the mapping's own claim
    assert result.email is None, "a column the mapping already claims for phone must not ALSO feed email's alias fallback"
    assert result.display_name == "Asha Rao"  # unrelated identity field, unaffected


# ---------------------------------------------------------------------------
# (c) server-side CanonicalField value validation
# ---------------------------------------------------------------------------


def test_confirm_mapping_rejects_invalid_canonical_field_value(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.agent.integration_agent import confirm_mapping, read_integration_state

    tid = _seed_tenant(substrate)
    with _ctx(uuid4(), tid):
        out = confirm_mapping.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id="google_sheet",
            mapping={"Mobile": "phone", "Notes": "not_a_real_canonical_field"},
        )
    assert out["confirmed"] is False
    assert "not_a_real_canonical_field" in out["error"]

    # Fail-closed on the WHOLE call — nothing persisted, not a partial write of the valid half.
    with _ctx(uuid4(), tid):
        state = read_integration_state.func(tenant_id=tid)  # type: ignore[attr-defined]
    assert state == {"phase": None, "current_connector_id": None, "pending_owner_input": None}


def test_confirm_mapping_accepts_every_real_canonical_field(substrate):  # type: ignore[no-untyped-def]
    """The positive control — every one of CanonicalField's own literals is accepted (the
    validator checks against the SAME source of truth the reasoner + LLM fallback use, never a
    hand-duplicated list that could drift)."""
    from orchestrator.agent.integration_agent import confirm_mapping
    from orchestrator.integrations.canonical_fields import GLOBAL_FIELD_HINTS

    tid = _seed_tenant(substrate)
    mapping = {f"col_{i}": cf for i, cf in enumerate(GLOBAL_FIELD_HINTS)}
    with _ctx(uuid4(), tid):
        out = confirm_mapping.func(  # type: ignore[attr-defined]
            tenant_id=tid, connector_id="google_sheet", mapping=mapping,
        )
    assert out == {
        "connector_id": "google_sheet", "confirmed": True, "field_count": len(mapping),
    }
