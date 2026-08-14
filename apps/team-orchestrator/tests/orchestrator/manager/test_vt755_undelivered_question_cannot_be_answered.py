"""VT-755 scope 0b — a question the owner never received must not consume their message.

THE DEFECT, from deployed dev 2026-08-14 (tenant d693785a). The whole owner conversation:

    12:19:13  owner      purane customers ko wapas laane ke liye ek accha sa offer draft kar do
    12:21:47  assistant  Got it — I'm on it and I'll update you shortly.
    12:23:35  owner      haan theek hai, bhej do unhe          <-- "yes fine, SEND IT TO THEM"
    12:28:25  assistant  Got it — I'm on it and I'll update you shortly.

`pending_questions` held a question asked at 12:19:58 that appears NOWHERE in that log — the table has
no emitter. At 12:26:07 `correlate_reply` bound the owner's 12:23:35 message to that invisible question
and stamped it `answered`. **Their instruction to send was recorded as clarification and discarded.**
Observed on 4 of 4 stalled tenants in the same re-drive.

The swallow lives in TWO places and closing one leaves the other live:

  1. `get_open` decides whether the turn routes to `answer_pending` at all;
  2. `correlate_reply` picks the oldest open question with no notion of delivery;
  3. and `triage_seam` returned `skip_legacy_dispatch=True` even when correlation FAILED — so the
     message was neither an answer nor an instruction. It simply vanished.

All three are guarded on `delivered_at IS NOT NULL` (migration 204). Since there is still no emitter,
every question is undelivered, so today an owner message falls through to normal dispatch — which is the
correct behaviour while the ask is undeliverable, and is exactly what would have saved "bhej do unhe".
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-755 delivery-gate tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) "
                "VALUES ('vt755 delivery gate', 'founding', 'trial') RETURNING id"
            ).fetchone()[0]
        )


def _insert_question(dsn: str, tenant_id: str, *, delivered: bool, text: str = "Which offer?") -> str:
    """`delivered_at` is set by SQL `now()`, never by a bound parameter — passing the STRING "now()"
    as a param would try to cast it to timestamptz and fail (or worse, in a laxer driver, store
    something meaningless and make the test lie about which branch it exercised)."""
    delivered_sql = "now()" if delivered else "NULL"
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO pending_questions "
                "(tenant_id, question_kind, question_text, status, delivered_at) "
                f"VALUES (%s, 'clarification', %s, 'open', {delivered_sql}) RETURNING id",  # noqa: S608 — literal from a bool, never user input
                (tenant_id, text),
            ).fetchone()[0]
        )


@pytest.mark.integration
def test_migration_204_added_the_column(dsn):
    """Cheap first assertion: everything below is vacuous if the column is missing."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pending_questions' AND column_name = 'delivered_at'"
        ).fetchone()
    assert row is not None, "migration 204 did not apply — pending_questions.delivered_at is absent"


@pytest.mark.integration
def test_an_UNDELIVERED_question_is_invisible_to_get_open(dsn, _dbpool, monkeypatch):
    """THE PRIMARY GATE. `triage_seam` reads `get_open` to decide whether this turn is an answer to a
    pending question. An undelivered question must not make that true, or the owner's message gets
    routed as an answer to something they never saw."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    from orchestrator.manager import pending_questions as pq

    tenant_id = _tenant(dsn)
    qid = _insert_question(dsn, tenant_id, delivered=False)

    assert pq.get_open(tenant_id) == [], (
        "an UNDELIVERED question is visible to get_open — the turn will route to answer_pending and "
        "swallow the owner's message, which is the VT-755 defect"
    )
    # ...but it must still be findable by the seam that will eventually SEND it, and by forensics.
    undelivered = pq.get_open(tenant_id, include_undelivered=True)
    assert [str(q["id"]) for q in undelivered] == [qid], (
        "the row must still exist and be reachable with include_undelivered=True — an emitter has to "
        "be able to see what it has not sent yet"
    )


@pytest.mark.integration
def test_a_DELIVERED_question_is_visible_and_answerable(dsn, _dbpool, monkeypatch):
    """The other direction. The fix must not break the real flow: once the owner has actually received
    a question, their reply IS its answer."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    from orchestrator.manager import pending_questions as pq

    tenant_id = _tenant(dsn)
    qid = _insert_question(dsn, tenant_id, delivered=True)

    assert [str(q["id"]) for q in pq.get_open(tenant_id)] == [qid]
    answered = pq.correlate_reply(tenant_id, "the 20% one", "SM-vt755-a")
    assert answered is not None and str(answered) == qid


@pytest.mark.integration
def test_correlate_reply_REFUSES_an_undelivered_question(dsn, _dbpool, monkeypatch):
    """DEFENCE IN DEPTH, and the assertion that speaks to the actual harm. This is the exact call that
    consumed "haan theek hai, bhej do unhe" on dev. It must return None so the caller falls through to
    dispatch and the instruction is read as an instruction."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    from orchestrator.manager import pending_questions as pq

    tenant_id = _tenant(dsn)
    qid = _insert_question(dsn, tenant_id, delivered=False)

    assert pq.correlate_reply(tenant_id, "haan theek hai, bhej do unhe", "SM-vt755-b") is None, (
        "the owner's SEND INSTRUCTION was consumed as the answer to a question they never received"
    )

    # And the row must be untouched — not answered, not silently mutated.
    with psycopg.connect(dsn, autocommit=True) as conn:
        status, answered_at, answer_text = conn.execute(
            "SELECT status, answered_at, answer_text FROM pending_questions WHERE id = %s", (qid,)
        ).fetchone()
    assert status == "open" and answered_at is None and answer_text is None, (
        "an undelivered question was mutated by a reply that cannot possibly have answered it"
    )


@pytest.mark.integration
def test_explicit_question_id_and_task_id_paths_are_guarded_too(dsn, _dbpool, monkeypatch):
    """`_select_open_question` has THREE branches (by question_id, by task_id, tenant-wide) and the
    seam calls it with an explicit question_id. Guarding only the tenant-wide branch would leave the
    path production actually uses wide open — which is how a one-line fix misses."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    from orchestrator.manager import pending_questions as pq

    tenant_id = _tenant(dsn)
    qid = _insert_question(dsn, tenant_id, delivered=False)

    assert pq.correlate_reply(tenant_id, "sure", "SM-1", question_id=qid) is None, "question_id branch"
    assert pq.correlate_reply(tenant_id, "sure", "SM-2", task_id=uuid4()) is None, "task_id branch"
    assert pq.correlate_reply(tenant_id, "sure", "SM-3") is None, "tenant-wide branch"


@pytest.mark.integration
def test_mark_delivered_is_what_flips_it_and_is_idempotent(dsn, _dbpool, monkeypatch):
    """The emitter's half of the contract. `delivered_at` is the fact that makes a question answerable,
    so it is stamped AFTER a successful send, never optimistically — stamping early would recreate the
    defect. Idempotent so a redelivered send cannot double-stamp."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    from orchestrator.manager import pending_questions as pq

    tenant_id = _tenant(dsn)
    qid = _insert_question(dsn, tenant_id, delivered=False)

    assert pq.get_open(tenant_id) == []
    assert pq.mark_delivered(tenant_id, qid) is True, "the first stamp must flip the row"
    assert pq.mark_delivered(tenant_id, qid) is False, "a second stamp must be a no-op"
    assert [str(q["id"]) for q in pq.get_open(tenant_id)] == [qid], (
        "once delivered, the question must become answerable"
    )
    assert pq.correlate_reply(tenant_id, "the 20% one", "SM-vt755-c") is not None
