"""VT-620 — ops-alert noise kill.

Two units under test here (no DB needed for the pure-unit ones; the name-match cases use a live
Postgres like the sibling VT-489 test):

  * ``_error_envelope_severity`` — a 'designed gate' error-envelope subtype (self-eval reject /
    budget ceiling / clarification) is 'warning'; anything else (a genuine crash / DB / data
    error) stays 'critical'. Pure unit.
  * ``_is_test_tenant`` — True for a synthetic ``convo-harness-…`` tenant, False for a real
    tenant, and FAIL-SAFE False on any read error (a real prod alert must never be suppressed).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")


# --- _error_envelope_severity (pure unit — no DB) --------------------------


def test_error_envelope_severity_designed_gate_is_warning():
    """A 'designed gate' subtype downgrades to warning (no page)."""
    from orchestrator.alerts.triggers import _error_envelope_severity

    for step in (
        "self_eval_rejected", "schema_rejection", "invalid_output_no_json",
        "invalid_variant_discriminator", "self_evaluate_seam_error", "agent_invalid_output",
        "model_output_conflict", "agent_hard_limit_breach", "agent_refusal",
        "owner_clarification_required",
    ):
        assert _error_envelope_severity(step) == "warning", step


def test_error_envelope_severity_genuine_error_stays_critical():
    """A genuine error (DB / unknown / unrecognised subtype) still pages."""
    from orchestrator.alerts.triggers import _error_envelope_severity

    assert _error_envelope_severity("database_error") == "critical"
    assert _error_envelope_severity("external_api_error") == "critical"
    assert _error_envelope_severity("unknown_error") == "critical"
    assert _error_envelope_severity("some_unmapped_code") == "critical"


def test_error_envelope_severity_none_and_empty_stay_critical():
    """NULL/empty step_name (the old 'unknown' page) stays critical — fail-loud."""
    from orchestrator.alerts.triggers import _error_envelope_severity

    assert _error_envelope_severity(None) == "critical"
    assert _error_envelope_severity("") == "critical"


def test_hard_limit_kind_is_now_warning():
    """VT-620: hard_limit (budget gate) downgraded critical -> warning."""
    from orchestrator.alerts.triggers import severity_for

    assert severity_for("hard_limit") == "warning"
    # escalation is deliberately LEFT critical (a genuine human escalation may page).
    assert severity_for("escalation") == "critical"


# --- _is_test_tenant fail-safe (pure unit — no DB) -------------------------


def test_is_test_tenant_failsafe_false_on_read_error(monkeypatch):
    """FAIL-SAFE: any read error → False (never suppress a real alert). Monkeypatch the pool to
    raise so no DB is needed."""
    from orchestrator.alerts import triggers as trig

    def _boom():
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(trig, "get_pool", _boom)
    assert trig._is_test_tenant(uuid4()) is False


# --- _is_test_tenant name match (live Postgres) ----------------------------

_DB = os.environ.get("DATABASE_URL")


@pytest.fixture(scope="module")
def pool():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool, name: str) -> str:  # type: ignore[no-untyped-def]
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, name),
        )
    return tid


@pytest.mark.skipif(not _DB, reason="DATABASE_URL not set — VT-620 name-match tests skipped")
def test_is_test_tenant_true_for_harness_name(pool):
    from orchestrator.alerts.triggers import _is_test_tenant

    tid = _tenant(pool, f"convo-harness-{uuid4().hex[:8]}")
    assert _is_test_tenant(UUID(tid)) is True


@pytest.mark.skipif(not _DB, reason="DATABASE_URL not set — VT-620 name-match tests skipped")
def test_is_test_tenant_false_for_real_name(pool):
    from orchestrator.alerts.triggers import _is_test_tenant

    tid = _tenant(pool, f"Sharma Sweets {uuid4().hex[:6]}")
    assert _is_test_tenant(UUID(tid)) is False


@pytest.mark.skipif(not _DB, reason="DATABASE_URL not set — VT-620 name-match tests skipped")
def test_harness_tenant_suppresses_slow_triggers(pool):
    """A convo-harness tenant returns an EMPTY slow-trigger set (whole sweep short-circuits)."""
    from orchestrator.alerts.triggers import detect_slow_triggers

    tid = _tenant(pool, f"convo-harness-{uuid4().hex[:8]}")
    assert detect_slow_triggers(UUID(tid)) == []
