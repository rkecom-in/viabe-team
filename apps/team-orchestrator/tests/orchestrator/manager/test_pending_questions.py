"""VT-527 (B4) — generic pending-questions: ask / correlate-reply / expire (live Postgres, RLS)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — pending_questions tests skipped",
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
            (tid, f"pq-{tid[:8]}"),
        )
    return tid


def _row(pool, qid):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT status, question_text, answer_text, last_message_sid "
            "FROM pending_questions WHERE id = %s",
            (str(qid),),
        ).fetchone()


def test_ask_creates_open_and_redacts(pool):
    from orchestrator.manager import pending_questions as pq

    tid = _seed_tenant(pool)
    qid = pq.ask(tid, "confirm we can reach the owner on +919876543210")
    row = _row(pool, qid)
    assert row["status"] == "open"
    assert "9876543210" not in row["question_text"]  # redacted before it hit the row


def test_ask_is_idempotent_per_task(pool):
    from orchestrator.manager import pending_questions as pq

    tid = _seed_tenant(pool)
    task = str(uuid4())
    a = pq.ask(tid, "which cohort?", task_id=task)
    b = pq.ask(tid, "which cohort (again)?", task_id=task)
    assert a == b  # a task holds at most one open question
    assert len(pq.get_open(tid, task_id=task)) == 1


def test_correlate_reply_answers_and_redacts(pool):
    from orchestrator.manager import pending_questions as pq

    tid = _seed_tenant(pool)
    qid = pq.ask(tid, "who is the target?")
    ret = pq.correlate_reply(tid, "message +919812345678 our regulars", "SM1")
    assert ret == qid
    row = _row(pool, qid)
    assert row["status"] == "answered"
    assert "9812345678" not in (row["answer_text"] or "")  # owner reply redacted


def test_correlate_reply_redelivery_is_noop(pool):
    from orchestrator.manager import pending_questions as pq

    tid = _seed_tenant(pool)
    qid = pq.ask(tid, "q?")
    pq.correlate_reply(tid, "first answer", "SMdup", question_id=qid)
    # a redelivered reply (same sid) must not double-answer; question is already answered anyway
    again = pq.correlate_reply(tid, "second answer", "SMdup", question_id=qid)
    assert again == qid
    assert _row(pool, qid)["status"] == "answered"


def test_correlate_reply_terminal_safe_first_answer_wins(pool):
    from orchestrator.manager import pending_questions as pq

    tid = _seed_tenant(pool)
    qid = pq.ask(tid, "q?")
    pq.correlate_reply(tid, "the real answer", "SMa", question_id=qid)
    # a later, different reply finds no OPEN question → no-op (None), first answer preserved
    assert pq.correlate_reply(tid, "late answer", "SMb", question_id=qid) is None
    row = _row(pool, qid)
    assert row["last_message_sid"] == "SMa"           # first answer's sid preserved
    assert row["answer_text"] == "the real answer"    # PII-free → redact is identity; not overwritten


def test_expire_stale_sweeps_past_ttl_only(pool):
    from orchestrator.manager import pending_questions as pq

    tid = _seed_tenant(pool)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    stale = pq.ask(tid, "stale?", expires_at=past)
    fresh = pq.ask(tid, "fresh?", expires_at=future)
    none_ttl = pq.ask(tid, "no ttl?")
    pq.expire_stale(pool=pool)
    assert _row(pool, stale)["status"] == "expired"
    assert _row(pool, fresh)["status"] == "open"
    assert _row(pool, none_ttl)["status"] == "open"


def test_tenant_isolation(pool):
    from orchestrator.manager import pending_questions as pq

    tid_a = _seed_tenant(pool)
    tid_b = _seed_tenant(pool)
    pq.ask(tid_a, "A only")
    assert pq.get_open(tid_b) == []  # RLS: B cannot see A's question
