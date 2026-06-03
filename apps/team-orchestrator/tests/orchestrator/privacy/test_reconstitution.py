"""VT-76 — opt-out 7-day reconstitution canary.

Pure-logic guards (sentinel, cron, SLA-trigger shape — no PII, critical) run with
no DB. The sweep itself is DB-gated on DATABASE_URL and exercised on SYNTHETIC
customer-referencing episodic rows (CL-422 synthetic; the real customer-referencing
events are VT-312, Blocked — so this proves the MECHANISM is correct + a no-op on
real data until then).

Acceptance (Cowork canary spec, ruling 20260604T033000Z):
- 7-day opted-out customer + synthetic customer episodic row → sweep →
  referenced_entity_id == sentinel + reconstitution_completed_at set + event ROW
  still exists (audit preserved);
- 6-day opt-out is NOT yet swept;
- 8-day un-reconstituted → reconstitution_sla_breach critical alert fires (and the
  tenant_alerts CHECK accepts the new kind);
- tenant-scoped (tenant A's reconstitution never touches tenant B);
- mig 089 clean-applies.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from orchestrator.privacy.reconstitution import (
    RECONSTITUTION_CRON,
    RECONSTITUTION_SENTINEL,
    RECONSTITUTION_SLA_DAYS,
    SLA_BREACH_TRIGGER_KIND,
    _build_sla_trigger,
)

# --- pure-logic guards (no DB) ----------------------------------------------


def test_sentinel_is_all_zeros():
    assert RECONSTITUTION_SENTINEL == UUID("00000000-0000-0000-0000-000000000000")


def test_cron_is_utc_for_4am_ist():
    # 22:30 UTC = 04:00 IST (no container TZ; DBOS fires on UTC).
    assert RECONSTITUTION_CRON == "30 22 * * *"


def test_sla_trigger_is_critical_and_pii_free():
    # _build_sla_trigger imports orchestrator.alerts.triggers, whose module-level
    # `from orchestrator.graph import get_pool` pulls in langgraph — absent from the
    # dep-less smoke runner. Skip there; the full (deps-installed) suite covers it.
    pytest.importorskip("langgraph")
    now = datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc)
    customer_id = str(uuid4())
    tenant_id = str(uuid4())
    row = {
        "customer_id": customer_id,
        "tenant_id": tenant_id,
        "opt_out_at": now - timedelta(days=9),
    }
    trig = _build_sla_trigger(row, now)
    assert trig.trigger_kind == SLA_BREACH_TRIGGER_KIND
    assert trig.severity == "critical"
    assert trig.payload["days_overdue"] == 9
    assert trig.payload["sla_days"] == RECONSTITUTION_SLA_DAYS
    # CL-390: ids + day-count only — no display_name / phone / email anywhere.
    blob = f"{trig.message_text} {trig.payload}"
    assert customer_id in blob  # the id IS allowed (not PII)
    for pii in ("@", "+91", "display_name", "phone"):
        assert pii not in blob


# --- DB-gated: the sweep on synthetic episodic rows -------------------------

_DB = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def _pool():
    """Build (once) the service-role pool the sweep + setup share."""
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _mk_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'recon-canary', 'founding', 'onboarding')", (tid,),
        )
    return tid


def _mk_opted_out_customer(pool, tenant_id: str, *, days_ago: int) -> str:
    """A synthetic opted-out customer with opt_out_at `days_ago` + 1h in the past
    (the +1h clears the boundary so the date math is skew-proof)."""
    cid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO customers (id, tenant_id, opt_out_status, opt_out_at) "
            "VALUES (%s, %s, 'opted_out', now() - make_interval(days => %s, hours => 1))",
            (cid, tenant_id, days_ago),
        )
    return cid


def _mk_customer_episodic(pool, tenant_id: str, customer_id: str) -> str:
    """A synthetic customer-referencing episodic row (what VT-312 will emit)."""
    eid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO episodic_events "
            "(id, tenant_id, event_type, referenced_entity_type, referenced_entity_id, occurred_at) "
            "VALUES (%s, %s, 'customer_dormant_threshold_crossed', 'customer', %s, now())",
            (eid, tenant_id, customer_id),
        )
    return eid


def _episodic(pool, event_id: str) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT referenced_entity_id::text AS ref, event_type "
            "FROM episodic_events WHERE id = %s", (event_id,),
        ).fetchone()


def _customer(pool, customer_id: str) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT reconstitution_completed_at FROM customers WHERE id = %s",
            (customer_id,),
        ).fetchone()


@pytest.fixture(scope="module")
def db():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    # mig 089 clean-applies (acceptance criterion).
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    return _pool()


@_DB
def test_seven_day_customer_is_reconstituted(db):
    from orchestrator.privacy.reconstitution import run_reconstitution_sweep_body

    tid = _mk_tenant(db)
    cid = _mk_opted_out_customer(db, tid, days_ago=7)
    eid = _mk_customer_episodic(db, tid, cid)

    result = run_reconstitution_sweep_body(now=datetime.now(timezone.utc))

    # The customer was reconstituted.
    assert UUID(cid) in result.reconstituted
    # The episodic ROW still exists (audit preserved) but the link is severed.
    ev = _episodic(db, eid)
    assert ev is not None  # row NOT deleted
    assert ev["ref"] == str(RECONSTITUTION_SENTINEL)
    # The completion stamp is set.
    assert _customer(db, cid)["reconstitution_completed_at"] is not None


@_DB
def test_six_day_customer_not_yet_swept(db):
    from orchestrator.privacy.reconstitution import run_reconstitution_sweep_body

    tid = _mk_tenant(db)
    cid = _mk_opted_out_customer(db, tid, days_ago=6)
    eid = _mk_customer_episodic(db, tid, cid)

    result = run_reconstitution_sweep_body(now=datetime.now(timezone.utc))

    assert UUID(cid) not in result.reconstituted
    assert _episodic(db, eid)["ref"] == cid  # link untouched
    assert _customer(db, cid)["reconstitution_completed_at"] is None


@_DB
def test_eight_day_unreconstituted_fires_sla_breach(db):
    """An opted-out customer still un-reconstituted 8+ days out is an SLA breach:
    the scan catches it, and dispatching the breach persists a
    reconstitution_sla_breach alert — proving the mig-089 CHECK accepts the new
    kind end-to-end. Dispatch is forced down the canary path (DEV bot, empty
    token → no real send) so the test is network-free."""
    from orchestrator.alerts.dispatch import dispatch_alert
    from orchestrator.privacy.reconstitution import (
        _build_sla_trigger,
        _scan_sla_breaches,
    )

    tid = _mk_tenant(db)
    cid = _mk_opted_out_customer(db, tid, days_ago=8)

    # 1. The scan flags the 8-day pending customer.
    breaches = _scan_sla_breaches(datetime.now(timezone.utc))
    breach = next((b for b in breaches if b["customer_id"] == cid), None)
    assert breach is not None

    # 2. Dispatching the breach persists a critical reconstitution_sla_breach
    #    alert (canary tenant → no real send; the persist proves the CHECK).
    os.environ["TEAM_CANARY_TENANT_IDS"] = tid
    try:
        alert_id = dispatch_alert(_build_sla_trigger(breach, datetime.now(timezone.utc)))
    finally:
        del os.environ["TEAM_CANARY_TENANT_IDS"]
    assert alert_id is not None  # persisted → CHECK accepted the new kind

    with db.connection() as conn:
        got = conn.execute(
            "SELECT trigger_kind, severity FROM tenant_alerts WHERE id = %s",
            (str(alert_id),),
        ).fetchone()
    assert got["trigger_kind"] == SLA_BREACH_TRIGGER_KIND
    assert got["severity"] == "critical"


@_DB
def test_reconstitution_is_tenant_scoped(db):
    """reconstitute_customer for tenant A must never touch tenant B's episodic
    footprint (RLS via tenant_connection)."""
    from orchestrator.privacy.reconstitution import reconstitute_customer

    tid_a = _mk_tenant(db)
    tid_b = _mk_tenant(db)
    cust_a = _mk_opted_out_customer(db, tid_a, days_ago=7)
    cust_b = _mk_opted_out_customer(db, tid_b, days_ago=7)
    ev_a = _mk_customer_episodic(db, tid_a, cust_a)
    ev_b = _mk_customer_episodic(db, tid_b, cust_b)

    reconstitute_customer(tid_a, cust_a, now=datetime.now(timezone.utc))

    assert _episodic(db, ev_a)["ref"] == str(RECONSTITUTION_SENTINEL)  # A severed
    assert _episodic(db, ev_b)["ref"] == cust_b  # B untouched
