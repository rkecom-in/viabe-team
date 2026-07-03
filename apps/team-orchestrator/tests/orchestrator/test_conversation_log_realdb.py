"""VT-579 — DB-backed substrate tests for the lifetime conversation log.

Requires a real Postgres + the dbos stack (the CI ``orchestrator`` job provisions pgvector/pg16; migration
164 applies via the fixture). Mirrors the substrate pattern in test_dsr_purge_substrate / test_journey:
migrations applied once, DBOS launched so the ``tenant_connection`` pool exists, tenants seeded via a
direct service-role (BYPASSRLS) connection. record_turn / active_window / search_history go through
``tenant_connection`` (the RLS'd app_role path); assertions read back via direct service-role SELECTs.

Covers: record + active_window (chronological), 24h cutoff, ≤20 cap, current-sid exclusion, idempotent
message_sid, search_history (ILIKE, newest-first), DSR purge coverage + cross-tenant isolation, the
journey double-write consistency, and the compaction trigger (distiller mocked).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-579 conversation-log substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``graph._pool`` (the substrate the ``tenant_connection`` path
    resolves) exists. Mirrors test_dsr_purge_substrate."""
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


def _new_tenant(dsn: str, *, name: str = "VT-579 conv-log test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, owner_phone) "
            "VALUES (%s, 'founding', 'paid_active', %s, %s) RETURNING id",
            (name, f"+9199{uuid4().int % 10**8:08d}", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _rows(dsn: str, tenant_id: UUID) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT role, text, message_sid, surface, created_at FROM conversation_log "
            "WHERE tenant_id = %s ORDER BY created_at ASC",
            (str(tenant_id),),
        ).fetchall()


def _insert_at(dsn: str, tenant_id: UUID, role: str, text: str, created_at: datetime, *, sid: str | None = None) -> None:
    """Service-role insert with an explicit created_at (for cutoff/cap/order tests)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO conversation_log (tenant_id, role, text, message_sid, surface, created_at) "
            "VALUES (%s, %s, %s, %s, 'manager', %s)",
            (str(tenant_id), role, text, sid, created_at),
        )


# --- record + active_window ----------------------------------------------------------------------


def test_record_and_active_window_chronological(substrate):
    from orchestrator.conversation_log import active_window, record_turn

    t = _new_tenant(substrate.dsn)
    record_turn(t, "owner", "hi", surface="manager")
    record_turn(t, "assistant", "hello", surface="manager")
    record_turn(t, "owner", "what's my plan", surface="manager")

    win = active_window(t)
    assert [w["role"] for w in win] == ["owner", "assistant", "owner"]  # chronological (oldest-first)
    assert [w["text"] for w in win] == ["hi", "hello", "what's my plan"]


def test_active_window_24h_cutoff(substrate):
    from orchestrator.conversation_log import active_window

    t = _new_tenant(substrate.dsn)
    now = datetime.now(UTC)
    _insert_at(substrate.dsn, t, "owner", "ancient", now - timedelta(hours=30))
    _insert_at(substrate.dsn, t, "owner", "recent", now - timedelta(hours=1))

    texts = [w["text"] for w in active_window(t)]
    assert "recent" in texts
    assert "ancient" not in texts  # older than 24h → excluded


def test_active_window_20_cap(substrate):
    from orchestrator.conversation_log import active_window

    t = _new_tenant(substrate.dsn)
    now = datetime.now(UTC)
    for i in range(25):
        _insert_at(substrate.dsn, t, "owner", f"turn-{i:02d}", now - timedelta(minutes=25 - i))

    win = active_window(t)
    assert len(win) == 20  # the ≤20 cap
    # the MOST RECENT 20 (turn-05 .. turn-24), chronological.
    assert win[0]["text"] == "turn-05"
    assert win[-1]["text"] == "turn-24"


def test_active_window_excludes_current_sid(substrate):
    from orchestrator.conversation_log import active_window, record_turn

    t = _new_tenant(substrate.dsn)
    record_turn(t, "owner", "prior", message_sid="SM_prior", surface="manager")
    record_turn(t, "owner", "current", message_sid="SM_current", surface="manager")

    texts = [w["text"] for w in active_window(t, exclude_message_sid="SM_current")]
    assert "prior" in texts
    assert "current" not in texts


def test_idempotent_message_sid(substrate):
    from orchestrator.conversation_log import record_turn

    t = _new_tenant(substrate.dsn)
    record_turn(t, "owner", "first", message_sid="SM_dup", surface="manager")
    record_turn(t, "owner", "redelivered", message_sid="SM_dup", surface="manager")

    rows = _rows(substrate.dsn, t)
    assert len(rows) == 1  # ON CONFLICT DO NOTHING collapsed the redelivery
    assert rows[0][1] == "first"


def test_null_sid_does_not_dedup(substrate):
    from orchestrator.conversation_log import record_turn

    t = _new_tenant(substrate.dsn)
    record_turn(t, "assistant", "ack one", surface="system")
    record_turn(t, "assistant", "ack two", surface="system")
    assert len(_rows(substrate.dsn, t)) == 2  # no sid → nothing to dedup on


# --- search_history ------------------------------------------------------------------------------


def test_search_history_ilike_newest_first(substrate):
    from orchestrator.conversation_log import search_history

    t = _new_tenant(substrate.dsn)
    now = datetime.now(UTC)
    _insert_at(substrate.dsn, t, "owner", "we sell SAREES and lehengas", now - timedelta(hours=2))
    _insert_at(substrate.dsn, t, "assistant", "noted — sarees it is", now - timedelta(hours=1))
    _insert_at(substrate.dsn, t, "owner", "unrelated chatter", now)

    hits = search_history(t, "saree")
    assert len(hits) == 2  # case-insensitive substring
    assert hits[0]["text"] == "noted — sarees it is"  # newest-first
    assert search_history(t, "") == []  # empty query → nothing


# --- DSR purge coverage --------------------------------------------------------------------------


def test_dsr_purge_covers_conversation_log(substrate):
    from orchestrator.conversation_log import record_turn
    from orchestrator.dsr_purge import _PURGE_ORDER, purge_tenant_data

    assert "conversation_log" in _PURGE_ORDER  # registered in the purge inventory

    subject = _new_tenant(substrate.dsn, name="DSR subject")
    other = _new_tenant(substrate.dsn, name="untouched tenant")
    record_turn(subject, "owner", "my private chat", surface="manager")
    record_turn(other, "owner", "should survive", surface="manager")

    # open a deletion ticket for the subject, run the purge.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        ticket = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status) "
            "VALUES (%s, 'deletion', 'open') RETURNING id",
            (str(subject),),
        ).fetchone()[0]
    result = purge_tenant_data(UUID(str(ticket)))

    assert result.deleted_counts.get("conversation_log", 0) >= 1
    assert _rows(substrate.dsn, subject) == []  # subject's conversation erased
    assert len(_rows(substrate.dsn, other)) == 1  # cross-tenant isolation preserved


# --- journey double-write consistency ------------------------------------------------------------


def test_journey_double_write_lands_in_shared_log(substrate):
    from orchestrator.onboarding.journey import _append_recent_turns, start_journey

    t = _new_tenant(substrate.dsn)
    start_journey(t, [{"field": "about", "kind": "gap", "prompt_en": "what do you do?"}])
    _append_recent_turns(t, {"role": "owner", "text": "we fix phones"}, {"role": "bot", "text": "great, where?"})

    rows = _rows(substrate.dsn, t)
    by_text = {r[1]: r for r in rows}
    assert "we fix phones" in by_text
    assert "great, where?" in by_text
    # 'bot' → 'assistant'; surface='journey'.
    assert by_text["we fix phones"][0] == "owner"
    assert by_text["great, where?"][0] == "assistant"
    assert by_text["great, where?"][3] == "journey"


# --- compaction ----------------------------------------------------------------------------------


def test_compaction_fires_distill_and_advances_watermark(substrate, monkeypatch):
    """Once >60 turns accumulate (a full 20-turn window + the 40 scroll-out threshold), _compact_run folds
    the scrolled-out head into the durable manager summary (distiller mocked) and advances the watermark
    so a re-run does not re-fold the same turns."""
    from orchestrator import conversation_log as clog

    t = _new_tenant(substrate.dsn)
    now = datetime.now(UTC)
    for i in range(65):
        _insert_at(substrate.dsn, t, "owner" if i % 2 == 0 else "assistant", f"m{i:02d}", now - timedelta(minutes=65 - i))

    captured: dict[str, Any] = {}

    def _fake_distill(tenant_id, evicted, prior):
        captured["evicted"] = evicted
        captured["prior"] = prior
        return "Durable summary of the early conversation."

    import orchestrator.onboarding.memory_distiller as md

    monkeypatch.setattr(md, "distill_evicted_turns", _fake_distill)

    clog._compact_run(t)

    # 65 turns - 20 in-window = 45 scrolled out (>= 40 trigger) → distilled.
    assert captured["evicted"], "distiller was not called"
    assert len(captured["evicted"]) == 45
    assert captured["prior"] is None  # first fold has no prior summary

    # the durable summary + watermark landed in agent_memory (agent='manager').
    assert clog.read_manager_summary(t) == "Durable summary of the early conversation."
    watermark = clog._read_manager_memory(t, clog._WATERMARK_KEY)
    assert watermark is not None

    # a re-run with no new turns folds nothing more (watermark now covers the scrolled-out head).
    captured.clear()
    clog._compact_run(t)
    assert not captured, "compaction re-folded already-summarised turns"
