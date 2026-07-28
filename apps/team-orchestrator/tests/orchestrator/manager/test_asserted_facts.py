"""VT-719 S3 — the asserted-facts ledger (CL-2026-07-28-single-voice-manager).

Unit layer (no DB): registry-only keys, deterministic contradiction semantics, fail-soft.
Real-DB layer (DATABASE_URL + RUN_INTEGRATION_TESTS, migration 187 via the substrate fixture):
record/read round-trip, same-value idempotence, append-only supersession chain, the O8 §12.3
card sweep read, cross-tenant RLS isolation, and DSR purge-order membership.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402

from orchestrator.manager import asserted_facts as af  # noqa: E402


# --- unit layer (no DB) --------------------------------------------------------------------


def test_unknown_fact_key_rejected(monkeypatch):
    """Free-text keys are rejected — two surfaces must never record the same commitment under
    different spellings and dodge the contradiction join."""
    assert af.record_assertion(uuid4(), "made_up_key", 1) is False
    assert af.contradiction_check(uuid4(), "made_up_key", 1) is None


def test_contradiction_check_no_prior(monkeypatch):
    monkeypatch.setattr(af, "active_assertion", lambda *a, **k: None)
    assert af.contradiction_check(uuid4(), "weekly_report_day", "monday") is None


def test_contradiction_check_same_value(monkeypatch):
    monkeypatch.setattr(
        af, "active_assertion",
        lambda *a, **k: {"fact_key": "weekly_report_day", "fact_value": "monday"},
    )
    assert af.contradiction_check(uuid4(), "weekly_report_day", "monday") is None


def test_contradiction_check_differing_returns_prior(monkeypatch):
    prior = {"fact_key": "weekly_report_day", "fact_value": "monday", "statement_text": "…Monday."}
    monkeypatch.setattr(af, "active_assertion", lambda *a, **k: prior)
    got = af.contradiction_check(uuid4(), "weekly_report_day", "friday")
    assert got is prior


def test_record_fail_soft_on_db_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("orchestrator.db.tenant_connection", _boom)
    assert af.record_assertion(uuid4(), "weekly_report_day", "monday") is False


def test_purge_order_membership():
    """Migration 187's table must be swept on DSR (the tenants row is anonymized, never deleted —
    CASCADE is not the erasure path)."""
    from orchestrator.dsr_purge import _PURGE_ORDER

    assert "manager_asserted_facts" in _PURGE_ORDER


# --- real-DB layer -------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-719 substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
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


def _new_tenant(dsn: str, *, name: str = "VT-719 asserted-facts test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, owner_phone) "
            "VALUES (%s, 'founding', 'paid_active', %s, %s) RETURNING id",
            (name, f"+9199{uuid4().int % 10**8:08d}", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
        return row[0]


@pytestmark_db
def test_record_roundtrip_and_idempotence(substrate):
    t = _new_tenant(substrate.dsn)
    assert af.record_assertion(t, "weekly_report_day", "monday", statement_text="Your report lands every Monday.") is True
    got = af.active_assertion(t, "weekly_report_day")
    assert got is not None and got["fact_value"] == "monday"
    # Same value again → no new row.
    assert af.record_assertion(t, "weekly_report_day", "monday") is True
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM manager_asserted_facts WHERE tenant_id = %s AND fact_key = %s",
            (str(t), "weekly_report_day"),
        ).fetchone()[0]
    assert n == 1


@pytestmark_db
def test_supersession_chain_append_only(substrate):
    t = _new_tenant(substrate.dsn)
    af.record_assertion(t, "weekly_report_day", "monday")
    assert af.contradiction_check(t, "weekly_report_day", "friday") is not None
    af.record_assertion(t, "weekly_report_day", "friday", statement_text="Earlier I said Monday — now Friday because…")
    got = af.active_assertion(t, "weekly_report_day")
    assert got is not None and got["fact_value"] == "friday"
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT status, superseded_by FROM manager_asserted_facts "
            "WHERE tenant_id = %s AND fact_key = %s ORDER BY asserted_at",
            (str(t), "weekly_report_day"),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "superseded" and rows[0][1] is not None  # old row flipped, linked
    assert rows[1][0] == "active"


@pytestmark_db
def test_cross_tenant_isolation(substrate):
    a, b = _new_tenant(substrate.dsn), _new_tenant(substrate.dsn)
    af.record_assertion(a, "trial_terms", {"months": 1, "auto_charge": False})
    assert af.active_assertion(b, "trial_terms") is None  # RLS: b never sees a's assertions


@pytestmark_db
def test_card_sweep_read(substrate):
    t = _new_tenant(substrate.dsn)
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        card = conn.execute(
            "INSERT INTO knowledge_cards "
            "(card_key, version, claim, claim_key, claim_value, distillation_note, "
            " authority, confidence, scope, status, retention_class) "
            "VALUES (gen_random_uuid(), 1, 'Lapsed means 45 days without a purchase', "
            " 'dormancy_window_days', "
            " '{\"value_type\": \"integer\", \"value\": 45}'::jsonb, "
            " 'VT-719 substrate test card', 'seed', 'high', 'global', 'validated', "
            " 'global_indefinite') RETURNING id"
        ).fetchone()[0]
    af.record_assertion(
        t, "dormancy_definition", "45d", derived_from_card_id=card,
        statement_text="A customer counts as lapsed after 45 days.",
    )
    hits = af.assertions_derived_from_card(t, card)
    assert len(hits) == 1 and hits[0]["fact_key"] == "dormancy_definition"
