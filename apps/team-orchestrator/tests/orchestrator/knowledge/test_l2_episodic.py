"""VT-66/67 — L2 episodic memory canary + substrate tests.

Live Postgres via DATABASE_URL. Proves the dual-projection exactly-once contract
(Cowork req-1), the retrieval path (VT-67), the Composer wire (`_build_ledger_summary`
reads L2 LIVE, not stale), tenant-scoping, and the VT-76 reconstitution hook.

Dual-projection model: the kg_events outbox is the single event stream; the drain
projects each event to BOTH L1 (entities/edges) AND L2 (episodic_events). Only the
overlapping types (campaign_sent, attribution_created) project to L2 here; the ~10
agent-decision L2 types get their own emit sites in VT-309.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — L2 episodic tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"l2-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def _l1_campaigns(pool, tid: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM l1_entities "
            "WHERE tenant_id = %s AND entity_type='campaign'",
            (tid,),
        ).fetchone()
    return int(row["n"])


def _l2_count(pool, tid: str, event_type: str | None = None) -> int:
    sql = "SELECT count(*) AS n FROM episodic_events WHERE tenant_id = %s"
    params: list = [tid]
    if event_type:
        sql += " AND event_type = %s"
        params.append(event_type)
    with pool.connection() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    return int(row["n"])


def _emit_campaign_sent(tid: str, campaign_id: str) -> str:
    """Emit a campaign_sent outbox event (returns its event_id)."""
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.kg_emit import emit_kg_event

    with tenant_connection(UUID(tid)) as conn, conn.transaction():
        eid = emit_kg_event(
            conn, "campaign_sent", tid,
            {"campaign_id": campaign_id, "customer_ids": [str(uuid4()), str(uuid4())]},
        )
    return str(eid)


# --- dual-projection exactly-once (Cowork req-1) -----------------------------


def test_emit_drain_projects_to_both_l1_and_l2(pool):
    from orchestrator.knowledge.kg_emit import drain_kg_events

    tid = _tenant(pool)
    cid = str(uuid4())
    _emit_campaign_sent(tid, cid)

    out = drain_kg_events(tid)
    assert out["drained"] == 1, out
    assert _l1_campaigns(pool, tid) == 1  # L1 projection
    assert _l2_count(pool, tid, "campaign_sent") == 1  # L2 projection


def test_idempotent_redrain_exactly_once_both(pool):
    from orchestrator.knowledge.kg_emit import drain_kg_events

    tid = _tenant(pool)
    _emit_campaign_sent(tid, str(uuid4()))
    drain_kg_events(tid)
    drain_kg_events(tid)  # re-drain — must not double-apply EITHER projection
    assert _l1_campaigns(pool, tid) == 1
    assert _l2_count(pool, tid, "campaign_sent") == 1


def test_partial_failure_l2_only_then_redrain_exactly_once(pool):
    """Crash AFTER L2 projection, BEFORE marking drained: re-drain must run the
    L1 projection and no-op L2 → exactly-once in both."""
    from orchestrator.knowledge.kg_emit import drain_kg_events
    from orchestrator.knowledge.l2_writer import record_episodic_event

    tid = _tenant(pool)
    cid = str(uuid4())
    eid = _emit_campaign_sent(tid, cid)
    # Simulate the L2-wrote-but-not-marked crash: project L2 by hand, leave
    # kg_events.drained_at NULL (no drain yet).
    record_episodic_event(
        tid, "campaign_sent",
        payload={"campaign_id": cid, "recipient_count": 2},
        referenced_entity_type="campaign", referenced_entity_id=cid, event_id=eid,
    )
    assert _l2_count(pool, tid, "campaign_sent") == 1
    # Re-drain: L1 projects fresh, L2 is a UNIQUE(tenant_id,event_id) no-op.
    drain_kg_events(tid)
    assert _l1_campaigns(pool, tid) == 1
    assert _l2_count(pool, tid, "campaign_sent") == 1  # still exactly one


def test_partial_failure_l1_only_then_redrain_exactly_once(pool):
    """Crash AFTER L1 projection, BEFORE marking drained: re-drain must run the
    L2 projection and no-op L1 → exactly-once in both."""
    from orchestrator.knowledge.kg_emit import drain_kg_events
    from orchestrator.knowledge.kg_population import KgEvent, process_kg_event

    tid = _tenant(pool)
    cid = str(uuid4())
    eid = _emit_campaign_sent(tid, cid)
    # Simulate the L1-wrote-but-not-marked crash: run the L1 consumer by hand,
    # leave kg_events undrained + no L2 row yet.
    process_kg_event(KgEvent(UUID(eid), "campaign_sent", UUID(tid),
                             {"campaign_id": cid, "customer_ids": []}))
    assert _l1_campaigns(pool, tid) == 1
    assert _l2_count(pool, tid, "campaign_sent") == 0
    # Re-drain: L1 is idempotent (kg_events_processed/external_key), L2 projects.
    drain_kg_events(tid)
    assert _l1_campaigns(pool, tid) == 1
    assert _l2_count(pool, tid, "campaign_sent") == 1


# --- retrieval (VT-67) -------------------------------------------------------


def test_retrieval_returns_drained_event(pool):
    from orchestrator.knowledge import l2_query
    from orchestrator.knowledge.kg_emit import drain_kg_events

    tid = _tenant(pool)
    cid = str(uuid4())
    _emit_campaign_sent(tid, cid)
    drain_kg_events(tid)

    events = l2_query.recent_events(tid, limit=10)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "campaign_sent"
    assert str(ev.referenced_entity_id) == cid
    assert "sent to 2 customers" in (ev.summary or "")


def test_referenced_entity_anonymization_ready(pool):
    """VT-76 hook: the episodic row references the campaign by id (the sweep's
    null-target) and is findable via events_for_entity."""
    from orchestrator.knowledge import l2_query
    from orchestrator.knowledge.kg_emit import drain_kg_events

    tid = _tenant(pool)
    cid = str(uuid4())
    _emit_campaign_sent(tid, cid)
    drain_kg_events(tid)

    hits = l2_query.events_for_entity(tid, cid)
    assert len(hits) == 1
    assert hits[0].referenced_entity_id is not None
    assert str(hits[0].referenced_entity_id) == cid


# --- Composer wire: _build_ledger_summary reads L2 LIVE ----------------------


def test_composer_ledger_summary_reads_l2_live(pool):
    """The L2→Composer wire is live, not stale: a recorded high-value threshold
    event surfaces in the bundle's customer_ledger_summary."""
    from orchestrator.context_builder import _build_ledger_summary
    from orchestrator.knowledge.l2_writer import record_episodic_event

    tid = _tenant(pool)
    customer = str(uuid4())
    record_episodic_event(
        tid, "customer_high_value_threshold_crossed",
        payload={"lifetime_paise": 500_000},
        referenced_entity_type="customer", referenced_entity_id=customer,
    )
    record_episodic_event(
        tid, "customer_dormant_threshold_crossed",
        payload={"cohort": "90d", "days_dormant": 95},
        referenced_entity_type="customer", referenced_entity_id=str(uuid4()),
    )

    summary, ok = _build_ledger_summary(UUID(tid))
    assert ok is True  # the read ran (wire live)
    assert str(customer) in [str(s) for s in summary.top_spenders]
    assert summary.dormant_cohorts.get("90d") == 1
    assert summary.total_customers == 2


# --- tenant-scoping (RLS + assert_tenant_scoped) -----------------------------


def test_retrieval_is_tenant_scoped(pool):
    from orchestrator.knowledge import l2_query
    from orchestrator.knowledge.kg_emit import drain_kg_events

    tid_a = _tenant(pool)
    tid_b = _tenant(pool)
    _emit_campaign_sent(tid_a, str(uuid4()))
    drain_kg_events(tid_a)

    assert len(l2_query.recent_events(tid_a, limit=10)) == 1
    assert len(l2_query.recent_events(tid_b, limit=10)) == 0  # B sees none of A
