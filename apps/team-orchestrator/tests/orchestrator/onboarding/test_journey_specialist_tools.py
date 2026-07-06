"""VT-609 (Loop Package 4) — DB-backed behavioral tests for the onboarding_conductor SPECIALIST's
write-tool helpers added to ``orchestrator.onboarding.journey``: ``record_extracted_answer``,
``record_field_skip``, ``confirm_field_answer``. These are the deterministic functions the
specialist's ``extract_owner_answer`` / ``record_skip`` / ``record_answer`` / ``apply_correction``
tools delegate to — the SAME state (``onboarding_journey``) and the SAME promotion gate
(``confirm_draft``) the pre-VT-609 interceptor uses, just without touching cursor/question_queue.

Mirrors the substrate pattern in ``test_journey.py`` / ``test_journey_paced_flow.py``: migrations
applied once, DBOS launched so the ``tenant_connection`` pool exists, tenants seeded via a direct
service-role (BYPASSRLS) psycopg connection.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-609 journey specialist-tool tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
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


def _new_tenant(dsn: str, *, name: str, business_type: str = "restaurant") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES (%s, 'founding', 'trial', now(), %s, %s) RETURNING id",
            (name, business_type, f"+9199{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _start_active_journey(dsn: str, tenant_id: UUID) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO onboarding_journey (tenant_id, status, question_queue, cursor, "
            "answers, skipped) VALUES (%s, 'active', '[]'::jsonb, 0, '{}'::jsonb, '[]'::jsonb)",
            (str(tenant_id),),
        )


def _journey_row(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, cursor, answers, skipped FROM onboarding_journey WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return {"status": row[0], "cursor": row[1], "answers": dict(row[2] or {}), "skipped": list(row[3] or [])}


def _canonical_profile_attributes(dsn: str, tenant_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE tenant_id = %s AND entity_type = 'business_profile'",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    return dict(row[0] or {})


# --- record_extracted_answer -------------------------------------------------------------------


def test_record_extracted_answer_records_without_promoting(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "hours", "9am-9pm")
    assert out == {"recorded": True, "field": "hours"}

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["hours"] == "9am-9pm"
    assert row["cursor"] == 0  # cursor/question_queue untouched (VT-609 — no queue to walk)
    # Not promoted to canonical — extract_owner_answer is the UNCONFIRMED gap-fill path.
    assert _canonical_profile_attributes(substrate.dsn, tenant) is None


def test_record_extracted_answer_rejects_bare_negative_value(substrate):  # type: ignore[no-untyped-def]
    """Defense-in-depth: a bare 'no'/'nope' passed AS the value is never recorded as fact (mirrors
    the walker's own is_bare_no_confirm / _is_bare_greeting guards)."""
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract bare-no")
    _start_active_journey(substrate.dsn, tenant)

    out = record_extracted_answer(tenant, "hours", "no")
    assert out == {"recorded": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "hours" not in row["answers"]


def test_record_extracted_answer_no_op_on_inactive_journey(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import record_extracted_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 extract inactive")
    # No journey row at all.
    out = record_extracted_answer(tenant, "hours", "9am-9pm")
    assert out == {"recorded": False}


# --- record_field_skip --------------------------------------------------------------------------


def test_record_field_skip_defers_field(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import record_field_skip

    tenant = _new_tenant(substrate.dsn, name="VT-609 skip")
    _start_active_journey(substrate.dsn, tenant)

    out = record_field_skip(tenant, "website")
    assert out == {"recorded": True, "field": "website"}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["skipped"] == ["website"]

    # Idempotent — skipping the same field twice does not duplicate the entry.
    out2 = record_field_skip(tenant, "website")
    assert out2 == {"recorded": True, "field": "website"}
    row2 = _journey_row(substrate.dsn, tenant)
    assert row2 is not None
    assert row2["skipped"] == ["website"]


# --- confirm_field_answer -----------------------------------------------------------------------


def test_confirm_field_answer_promotes_valid_business_type(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm valid")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "Pune")
    assert out == {"recorded": True, "promoted": True, "field": "city"}

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["city"] == "Pune"
    attrs = _canonical_profile_attributes(substrate.dsn, tenant)
    assert attrs is not None
    assert attrs["city"] == "Pune"


def test_confirm_field_answer_never_asserts_off_taxonomy_business_type(substrate):  # type: ignore[no-untyped-def]
    """CL-390 never-assert: an off-taxonomy business_type is recorded as a plain answer (the
    conversation substrate) but NEVER promoted to the canonical profile as fact."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm off-taxonomy")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "business_type", "not-a-real-taxonomy-value-xyz")
    assert out["recorded"] is True
    assert out["promoted"] is False

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["business_type"] == "not-a-real-taxonomy-value-xyz"
    # NEVER asserted as canonical fact.
    attrs = _canonical_profile_attributes(substrate.dsn, tenant)
    assert attrs is None or "business_type" not in attrs


def test_confirm_field_answer_rejects_bare_negative_value(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 confirm bare-no")
    _start_active_journey(substrate.dsn, tenant)

    out = confirm_field_answer(tenant, "city", "nope")
    assert out == {"recorded": False, "promoted": False}
    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert "city" not in row["answers"]


def test_confirm_field_answer_correction_overwrites_prior_value(substrate):  # type: ignore[no-untyped-def]
    """apply_correction (the agent tool) calls this exact function — an owner correction always
    overwrites a prior (confirmed or populated) value, mirroring populate-first's "edits-forever"
    invariant."""
    from orchestrator.onboarding.journey import confirm_field_answer

    tenant = _new_tenant(substrate.dsn, name="VT-609 correction")
    _start_active_journey(substrate.dsn, tenant)

    confirm_field_answer(tenant, "city", "Mumbai")
    out = confirm_field_answer(tenant, "city", "Pune")
    assert out == {"recorded": True, "promoted": True, "field": "city"}

    row = _journey_row(substrate.dsn, tenant)
    assert row is not None
    assert row["answers"]["city"] == "Pune"
    attrs = _canonical_profile_attributes(substrate.dsn, tenant)
    assert attrs is not None
    assert attrs["city"] == "Pune"
