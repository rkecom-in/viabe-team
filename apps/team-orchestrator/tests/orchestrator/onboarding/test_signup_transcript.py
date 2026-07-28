"""VT-714 — pre-tenant transcript capture + flush (dep-less, pool-mocked).

Fazal: "Journeys must be captured even when the user is new, not-logged in state … and those
actions must be flagged as non-logged in state."
"""
from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import whatsapp_signup as ws  # noqa: E402

_PHONE = "+15550990001"


class _Conn:
    def __init__(self, store: dict[str, Any]):
        self.store = store

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN201
        self.store.setdefault("sql", []).append((" ".join(sql.split()), params))

        class _Cur:
            rowcount = 1

            def fetchone(_self):  # noqa: ANN202
                if "SELECT transcript" in sql:
                    return (self.store.get("transcript") or [],)
                return None

        return _Cur()


class _Pool:
    def __init__(self, store):
        self.store = store

    def connection(self):  # noqa: ANN201
        conn = _Conn(self.store)

        class _CM:
            def __enter__(_s):  # noqa: ANN202
                return conn

            def __exit__(_s, *a):  # noqa: ANN202
                return False

        return _CM()


@pytest.fixture()
def store(monkeypatch):
    st: dict[str, Any] = {}
    monkeypatch.setattr(ws, "_pool", lambda: _Pool(st))
    return st


def test_append_transcript_writes_entry(store) -> None:
    ws._append_transcript(_PHONE, "owner", "Hi", "SM1")
    sql, params = store["sql"][-1]
    assert "SET transcript = transcript ||" in sql
    entry = json.loads(params[0])[0]
    assert entry["role"] == "owner" and entry["text"] == "Hi" and entry["sid"] == "SM1"
    assert entry["ts"], "original timestamp captured"


def test_flush_writes_signup_surface_rows(store) -> None:
    store["transcript"] = [
        {"role": "owner", "text": "Hi", "sid": "SM1", "ts": "2026-07-28T10:00:00+00:00"},
        {"role": "assistant", "text": "consent card", "sid": None, "ts": "2026-07-28T10:00:05+00:00"},
    ]
    n = ws.flush_transcript_to_tenant(_PHONE, "77777777-7777-7777-7777-777777777777")
    assert n == 2
    inserts = [x for x in store["sql"] if "INSERT INTO conversation_log" in x[0]]
    assert len(inserts) == 2
    sql, params = inserts[0]
    assert "'signup'" in sql, "flagged as the not-logged-in surface"
    assert params[4] == "2026-07-28T10:00:00+00:00", "original ts preserved"


def test_flush_fail_soft(store, monkeypatch) -> None:
    monkeypatch.setattr(ws, "_pool", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert ws.flush_transcript_to_tenant(_PHONE, "77777777-7777-7777-7777-777777777777") == 0
