"""VT-297 — Telegram inbound identity-binding substrate (real Postgres).

Migration 076 adds operator_telegram.telegram_user_id + a partial-UNIQUE on the VERIFIED rows.
This is the substrate behind team-web's resolveOperatorFromTelegram (the IDOR-crux). Real PG
(no mocks) proves: (1) the verified-only resolution query returns the operator iff verified;
(2) the partial-unique blocks two operators sharing one VERIFIED telegram account (takeover
guard) while allowing the same id on unverified rows. Gated on DATABASE_URL + dbos; CL-422.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-297 inbound-identity canary skipped",
)


@pytest.fixture(scope="module")
def dsn():  # type: ignore[no-untyped-def]
    import apply_migrations

    d = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=d)["failed"]
    return d


def _operator(dsn: str) -> str:
    op = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO operator_allowlist (user_id) VALUES (%s)", (op,))
    return op


def _bind(dsn: str, op: str, chat: str, tg_user: int | None, *, verified: bool) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_telegram (operator_id, chat_id, telegram_user_id, verified_at) "
            "VALUES (%s, %s, %s, %s)",
            (op, chat, tg_user, "now()" if verified else None),
        )


def _resolve(dsn: str, tg_user: int) -> str | None:
    """The exact verified-only resolution team-web's identity.ts runs."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT operator_id FROM operator_telegram "
            "WHERE telegram_user_id = %s AND verified_at IS NOT NULL",
            (tg_user,),
        ).fetchone()
    return str(row[0]) if row else None


def test_migration_076_added_column_and_index(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        col = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='operator_telegram' AND column_name='telegram_user_id'"
        ).fetchone()
        idx = conn.execute(
            "SELECT 1 FROM pg_indexes "
            "WHERE tablename='operator_telegram' AND indexname='uq_operator_telegram_user_verified'"
        ).fetchone()
    assert col is not None
    assert idx is not None


def test_verified_binding_resolves_unverified_does_not(dsn):
    op_v = _operator(dsn)
    op_u = _operator(dsn)
    tg_v = uuid4().int % 10**9
    tg_u = uuid4().int % 10**9
    _bind(dsn, op_v, "C-V", tg_v, verified=True)
    _bind(dsn, op_u, "C-U", tg_u, verified=False)  # unverified → must NOT resolve

    assert _resolve(dsn, tg_v) == op_v
    assert _resolve(dsn, tg_u) is None          # the IDOR-crux: unverified reaches nothing
    assert _resolve(dsn, uuid4().int % 10**9) is None  # unknown id reaches nothing


def test_partial_unique_blocks_two_operators_sharing_verified_account(dsn):
    op_a = _operator(dsn)
    op_b = _operator(dsn)
    tg = uuid4().int % 10**9
    _bind(dsn, op_a, "C-A", tg, verified=True)
    # A SECOND operator verifying the SAME telegram account must be rejected (takeover guard).
    with pytest.raises(psycopg.errors.UniqueViolation):
        _bind(dsn, op_b, "C-B", tg, verified=True)


def test_same_id_allowed_on_unverified_rows(dsn):
    op_a = _operator(dsn)
    op_b = _operator(dsn)
    tg = uuid4().int % 10**9
    # Two UNVERIFIED rows with the same id are allowed (partial index only constrains verified).
    _bind(dsn, op_a, "C-A", tg, verified=False)
    _bind(dsn, op_b, "C-B", tg, verified=False)  # must NOT raise
    assert _resolve(dsn, tg) is None  # neither verified → resolves to nothing
