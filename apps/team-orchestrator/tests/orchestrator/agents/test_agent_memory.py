"""VT-550 (C3b) — the seedable learnable-memory MECHANISM (live Postgres, RLS-enforced).

Proves the mechanism + the seed-then-learn-beyond posture: a learned entry OVERWRITES the seed
(grow-beyond); global seeds are readable by any tenant but tenant-unwritable; retrieval is
default-closed until activated; content is PII-redacted at write.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — agent_memory tests skipped",
)


@pytest.fixture(scope="module")
def pool():
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


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"tm-{tid[:8]}"),
        )
    return tid


def _v(row, key, idx):
    return row[key] if isinstance(row, dict) else row[idx]


def test_tenant_learned_overwrites_seed(pool):
    """The core posture: a learned entry OVERWRITES the seed for the same key (version+1), not append."""
    from orchestrator.agents import agent_memory as m
    from orchestrator.db import tenant_connection

    tid = _seed_tenant(pool)
    m.upsert_seed(scope="tenant", tenant_id=tid, memory_key="tone", content="seed: be formal")
    m.upsert_learned(tid, memory_key="tone", content="learned: owner likes short warm messages")
    with tenant_connection(tid) as c:
        rows = c.execute(
            "SELECT source, content, version FROM agent_memory "
            "WHERE tenant_id = %s AND memory_key = 'tone'",
            (tid,),
        ).fetchall()
    assert len(rows) == 1  # overwrite, not append
    assert _v(rows[0], "source", 0) == "learned"  # learned supersedes seed
    assert "short warm" in _v(rows[0], "content", 1)
    assert _v(rows[0], "version", 2) == 2  # grew beyond the seed


def test_global_seed_readable_by_any_tenant(pool):
    from orchestrator.agents import agent_memory as m
    from orchestrator.db import tenant_connection

    m.upsert_seed(
        scope="global", archetype="retail_kirana", memory_key="greeting",
        content="a warm regional greeting lifts reply rate",
    )
    tid = _seed_tenant(pool)
    with tenant_connection(tid) as c:
        rows = c.execute(
            "SELECT content FROM agent_memory "
            "WHERE tenant_id IS NULL AND archetype = 'retail_kirana' AND memory_key = 'greeting'"
        ).fetchall()
    assert rows and "greeting" in _v(rows[0], "content", 0)


def test_retrieval_default_closed_then_activated(pool):
    from orchestrator.agents import agent_memory as m

    tid = _seed_tenant(pool)
    m.upsert_learned(tid, memory_key="cadence", content="follow up in 3 days")
    assert m.get_active_memory(tid) == []  # default-closed — nothing retrievable yet
    assert m.mark_retrievable(tid, memory_key="cadence") == 1  # Phase-2 activation flip
    active = m.get_active_memory(tid)
    assert any(e["memory_key"] == "cadence" and "3 days" in e["content"] for e in active)


def test_tenant_cannot_write_global_seed(pool):
    """Security posture — a tenant conn cannot insert a global (tenant_id NULL) row (RLS INSERT check)."""
    import psycopg

    from orchestrator.db import tenant_connection

    tid = _seed_tenant(pool)
    with tenant_connection(tid) as c, pytest.raises(psycopg.errors.Error):
        c.execute(
            "INSERT INTO agent_memory (tenant_id, memory_scope, source, memory_key, content) "
            "VALUES (NULL, 'global', 'seed', 'x', 'y')"
        )


def test_content_pii_redacted_at_write(pool):
    from orchestrator.agents import agent_memory as m

    tid = _seed_tenant(pool)
    m.upsert_learned(tid, memory_key="pii", content="call the owner on +919876543210 tomorrow")
    m.mark_retrievable(tid, memory_key="pii")
    entry = next(e for e in m.get_active_memory(tid) if e["memory_key"] == "pii")
    assert "9876543210" not in entry["content"]


# --- VT-556: the VTR teach-loop directive (provenance + authority + immediate retrieval) ----------


def test_directive_ingest_carries_provenance_and_is_retrievable(pool):
    """A VTR directive lands as a learned row with provenance (operator id + authority='vtr') AND
    is immediately retrieval-eligible — the teach→manager-picks-it-up posture (no separate flip)."""
    from orchestrator.agents import agent_memory as m
    from orchestrator.db import tenant_connection

    tid = _seed_tenant(pool)
    op = str(uuid4())
    version = m.upsert_directive(
        tid, memory_key="strategy:winback",
        content="prioritise dormant high-value customers this week",
        authored_by_operator_id=op, agent="manager", authority="vtr",
    )
    assert version == 1
    with tenant_connection(tid) as c:
        row = c.execute(
            "SELECT source, authority, authored_by_operator_id, retrieval_eligible, content "
            "FROM agent_memory WHERE tenant_id = %s AND memory_key = 'strategy:winback'",
            (tid,),
        ).fetchone()
    assert _v(row, "source", 0) == "learned"
    assert _v(row, "authority", 1) == "vtr"
    assert str(_v(row, "authored_by_operator_id", 2)) == op  # WHO taught it
    assert _v(row, "retrieval_eligible", 3) is True  # manager picks it up next run
    # retrievable via the manager's agent-scoped read (agent IN ('manager','')), authority surfaced.
    active = m.get_active_memory(tid, agent="manager")
    hit = next(e for e in active if e["memory_key"] == "strategy:winback")
    assert hit["authority"] == "vtr"
    assert "dormant" in hit["content"]


def test_directive_overwrite_bumps_version(pool):
    from orchestrator.agents import agent_memory as m

    tid = _seed_tenant(pool)
    op = str(uuid4())
    m.upsert_directive(tid, memory_key="strategy:tone", content="be concise",
                       authored_by_operator_id=op)
    v2 = m.upsert_directive(tid, memory_key="strategy:tone", content="be concise and warm",
                            authored_by_operator_id=op)
    assert v2 == 2  # overwrite, version+1 (grow-beyond)


def test_directive_orders_before_plain_learned(pool):
    """The manager reads VTR directives FIRST (authority='vtr' DESC) ahead of self-learned entries."""
    from orchestrator.agents import agent_memory as m

    tid = _seed_tenant(pool)
    m.upsert_learned(tid, memory_key="lk", content="self-learned note", agent="manager")
    m.mark_retrievable(tid, memory_key="lk", agent="manager")
    m.upsert_directive(tid, memory_key="strategy:dk", content="vtr directive",
                       authored_by_operator_id=str(uuid4()), agent="manager")
    active = m.get_active_memory(tid, agent="manager")
    keys = [e["memory_key"] for e in active]
    assert keys.index("strategy:dk") < keys.index("lk")  # vtr directive first


def test_directive_content_pii_redacted(pool):
    from orchestrator.agents import agent_memory as m

    tid = _seed_tenant(pool)
    m.upsert_directive(tid, memory_key="strategy:pii",
                       content="ping owner at +919876543210", authored_by_operator_id=str(uuid4()))
    hit = next(e for e in m.get_active_memory(tid, agent="manager")
               if e["memory_key"] == "strategy:pii")
    assert "9876543210" not in hit["content"]
