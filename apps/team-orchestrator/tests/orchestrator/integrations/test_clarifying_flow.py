"""VT-53 — clarifying-question flow tests.

Two layers:
  * PURE: deterministic reply parsing + bundling validation (no DB, no LLM).
  * DB: open / record_reply / sweep_expired + cross-tenant RLS, against a live
    Postgres (DATABASE_URL), run in the CI ``orchestrator`` job. Mirrors
    test_tenant_isolation.py (SET ROLE app_role => FORCE RLS genuinely enforced).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.clarifying_flow import (  # noqa: E402
    ClarificationQuestion,
    TooManyQuestionsError,
    open_clarification,
    parse_amount_to_paise,
    parse_numeric,
)


# --- PURE: deterministic reply parsing ----------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1500", 1500),
        ("₹1,500", 1500),
        ("1500/-", 1500),
        ("१५००", 1500),            # Devanagari digits
        ("₹१,५००", 1500),          # Devanagari + ₹ + comma
        ("fifteen hundred", 1500),
        ("two thousand five hundred", 2500),
        ("one lakh", 100000),
        ("rs 250 only", 250),
        ("", None),
        ("no idea", None),         # unparseable -> None (never a guess, P4)
        ("   ", None),
    ],
)
def test_parse_numeric(raw, expected):
    assert parse_numeric(raw) == expected


@pytest.mark.parametrize(
    "raw,paise",
    [
        ("₹1500", 150000),
        ("fifteen hundred", 150000),
        ("१५००", 150000),
        ("garbage", None),
    ],
)
def test_parse_amount_to_paise(raw, paise):
    assert parse_amount_to_paise(raw) == paise


# --- PURE: bundling validation (no DB reached) --------------------------------

def test_open_clarification_rejects_more_than_three():
    qs = [ClarificationQuestion(field=f"f{i}", prompt=f"q{i}?") for i in range(4)]
    with pytest.raises(TooManyQuestionsError):
        open_clarification("11111111-1111-4111-8111-111111111111", "upload-1", qs)


def test_open_clarification_rejects_zero():
    with pytest.raises(ValueError):
        open_clarification("11111111-1111-4111-8111-111111111111", "upload-1", [])


# --- DB: persistence + cross-tenant RLS ---------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — clarifying-flow DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
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


def _new_tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-53 clarify test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    return str(row[0])


@_DB
def test_open_and_readback(db_ctx):
    from orchestrator.db import tenant_connection

    tenant = _new_tenant(db_ctx.dsn)
    cid = open_clarification(
        tenant, "upload-A",
        [ClarificationQuestion(field="balance", prompt="₹1200 or ₹1500?")],
    )
    with tenant_connection(tenant) as conn:
        row = conn.execute(
            "SELECT status, jsonb_array_length(questions) AS nq FROM "
            "pending_clarifications WHERE id = %s", (str(cid),)
        ).fetchone()
    assert row["status"] == "pending"
    assert row["nq"] == 1


@_DB
def test_record_reply_idempotent(db_ctx):
    tenant = _new_tenant(db_ctx.dsn)
    cid = open_clarification(
        tenant, "upload-B",
        [ClarificationQuestion(field="balance", prompt="how much?")],
    )
    from orchestrator.integrations.clarifying_flow import record_reply

    assert record_reply(tenant, cid, {"balance": 150000}) is True
    # second call: already answered -> no pending row -> False.
    assert record_reply(tenant, cid, {"balance": 150000}) is False


@_DB
def test_sweep_expires_only_overdue(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.clarifying_flow import sweep_expired

    tenant = _new_tenant(db_ctx.dsn)
    eight_days_ago = datetime.now(UTC) - timedelta(days=8)
    overdue = open_clarification(
        tenant, "old", [ClarificationQuestion(field="x", prompt="?")],
        now=eight_days_ago,  # expires = 8d-ago + 7d = 1d ago
    )
    fresh = open_clarification(
        tenant, "new", [ClarificationQuestion(field="y", prompt="?")],
    )
    n = sweep_expired(tenant, now=datetime.now(UTC))
    assert n == 1
    with tenant_connection(tenant) as conn:
        statuses = {
            r["id"]: r["status"]
            for r in conn.execute(
                "SELECT id, status FROM pending_clarifications WHERE id = ANY(%s)",
                ([str(overdue), str(fresh)],),
            ).fetchall()
        }
    assert statuses[overdue] == "expired"
    assert statuses[fresh] == "pending"


@_DB
def test_cross_tenant_cannot_resolve(db_ctx):
    """Tenant B cannot resolve tenant A's clarification (RLS); real count backstop."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.clarifying_flow import record_reply

    tenant_a = _new_tenant(db_ctx.dsn)
    tenant_b = _new_tenant(db_ctx.dsn)
    cid = open_clarification(
        tenant_a, "upload-A",
        [ClarificationQuestion(field="balance", prompt="?")],
    )
    # B's RLS-scoped UPDATE matches no row -> False (cannot resolve A's).
    assert record_reply(tenant_b, cid, {"balance": 1}) is False
    # Real backstop: B sees ZERO of A's rows; A still sees its own as pending.
    with tenant_connection(tenant_b) as conn:
        b_sees = conn.execute(
            "SELECT count(*) AS n FROM pending_clarifications WHERE id = %s",
            (str(cid),),
        ).fetchone()["n"]
    assert b_sees == 0, "RLS leak: tenant B saw tenant A's clarification"
    with tenant_connection(tenant_a) as conn:
        a_status = conn.execute(
            "SELECT status FROM pending_clarifications WHERE id = %s", (str(cid),)
        ).fetchone()["status"]
    assert a_status == "pending", "A's row wrongly mutated by B"
