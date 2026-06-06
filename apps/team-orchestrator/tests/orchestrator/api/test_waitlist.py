"""VT-97 — /api/waitlist canary (Rule #15, real PG). Pre-tenant waitlist capture + its OWN
erasure path: insert + dedup-idempotent + consent-gated + X-Internal-Secret + the DELETE
erasure (hard-delete) + the retention/post-notify purge fns. CL-422 synthetic data only."""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-97 waitlist canary skipped",
)

_SECRET = "vt97-test-secret"
_HDR = {"X-Internal-Secret": _SECRET}


@pytest.fixture(scope="module")
def pool():
    import apply_migrations
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield graph_mod._pool
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev


@pytest.fixture
def client(pool, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.waitlist import router

    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _email() -> str:
    return f"w{uuid.uuid4().hex[:12]}@example.com"


def _wa() -> str:
    return "+919" + str(uuid.uuid4().int)[:9]


def _count(pool, email: str) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM waitlist_signups WHERE email = %s", (email,)
        ).fetchone()["n"]


def _body(email: str, wa: str, **kw):
    return {"email": email, "whatsapp_e164": wa, "consent": True, **kw}


def test_join_queued_and_persists_consent(client, pool):
    email, wa = _email(), _wa()
    r = client.post("/api/waitlist", json=_body(email, wa), headers=_HDR)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT consent_at, notified_at FROM waitlist_signups WHERE email = %s", (email,)
        ).fetchone()
    assert row is not None and row["consent_at"] is not None  # consent stamped at collection
    assert row["notified_at"] is None


def test_dedup_idempotent_no_enumeration(client, pool):
    email, wa = _email(), _wa()
    assert client.post("/api/waitlist", json=_body(email, wa), headers=_HDR).status_code == 200
    # re-submit (same email) → still "queued" (never leaks that it already existed), 1 row
    r2 = client.post("/api/waitlist", json=_body(email, _wa()), headers=_HDR)
    assert r2.status_code == 200 and r2.json()["status"] == "queued"
    assert _count(pool, email) == 1


def test_consent_false_rejected(client, pool):
    email = _email()
    r = client.post("/api/waitlist", json=_body(email, _wa(), consent=False), headers=_HDR)
    assert r.status_code == 400
    assert _count(pool, email) == 0  # no row without consent


def test_internal_secret_required(client):
    body = _body(_email(), _wa())
    assert client.post("/api/waitlist", json=body).status_code == 403
    assert client.post("/api/waitlist", json=body, headers={"X-Internal-Secret": "x"}).status_code == 403


def test_bad_email_and_phone_rejected(client):
    assert client.post("/api/waitlist", json=_body("notanemail", _wa()), headers=_HDR).status_code == 400
    assert client.post("/api/waitlist", json=_body(_email(), "+12025551234"), headers=_HDR).status_code == 400


def test_delete_erasure_hard_deletes(client, pool):
    email, wa = _email(), _wa()
    client.post("/api/waitlist", json=_body(email, wa), headers=_HDR)
    assert _count(pool, email) == 1
    r = client.request("DELETE", "/api/waitlist", params={"email": email}, headers=_HDR)
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert _count(pool, email) == 0  # actually gone (hard delete)
    # erasure also needs the secret
    assert client.request("DELETE", "/api/waitlist", params={"email": email}).status_code == 403


def test_purge_fns(client, pool):
    from orchestrator.api.waitlist import purge_notified_waitlist, purge_stale_unnotified

    email_n, email_old = _email(), _email()
    client.post("/api/waitlist", json=_body(email_n, _wa()), headers=_HDR)
    client.post("/api/waitlist", json=_body(email_old, _wa()), headers=_HDR)
    with pool.connection() as conn:
        conn.execute("UPDATE waitlist_signups SET notified_at = now() WHERE email = %s", (email_n,))
        conn.execute(
            "UPDATE waitlist_signups SET created_at = now() - interval '1 year' WHERE email = %s",
            (email_old,),
        )
    assert purge_notified_waitlist() >= 1
    assert _count(pool, email_n) == 0  # post-notify purge
    assert purge_stale_unnotified(months=6) >= 1
    assert _count(pool, email_old) == 0  # retention bound
