"""VT-359 (VT-357-p3 minimal) — the ops resolve-escalation endpoint. Real-PG.

Marks the escalation resolved + ops-audits + best-effort support_resolved send (mocked). Same
internal-secret + operator-JWT gate as resolve-phone.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

_SECRET = "vt359-internal"
_OP_SECRET = "vt359-operator-jwt"


@pytest.fixture(scope="module")
def _dbpool():
    db_url = os.environ["DATABASE_URL"]
    import apply_migrations

    assert not apply_migrations.apply(dsn=db_url)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = db_url
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _seed_escalation(pool) -> tuple[str, str]:
    with pool.connection() as conn:
        tid = str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-359', 'founding', 'paid_active') RETURNING id"
        ).fetchone()["id"])
        eid = str(conn.execute(
            "INSERT INTO escalations (tenant_id, kind, severity, status) "
            "VALUES (%s, 'support_fallback', 'medium', 'open') RETURNING id",
            (tid,),
        ).fetchone()["id"])
    return tid, eid


def _op_jwt(operator_id: str) -> str:
    import jwt as pyjwt

    now = int(time.time())
    return pyjwt.encode(
        {"operator_claim": True, "operator_id": operator_id, "aud": "authenticated",
         "iat": now, "exp": now + 300},
        _OP_SECRET, algorithm="HS256",
    )


def test_resolve_escalation_marks_and_audits(_dbpool, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _OP_SECRET)
    sent: list[tuple] = []
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_template_message",
        lambda tid, name, params, **kw: sent.append((name, params)),
    )
    from orchestrator.api.ops_resolve import ResolveEscalationBody, resolve_escalation

    tid, eid = _seed_escalation(_dbpool)
    operator = str(uuid4())
    out = resolve_escalation(
        ResolveEscalationBody(escalation_id=eid, operator_id=operator, resolution_reason="fixed it"),
        x_internal_secret=_SECRET,
        x_operator_jwt=_op_jwt(operator),
    )
    assert out["status"] == "resolved" and out["tenant_id"] == tid
    with _dbpool.connection() as conn:
        row = conn.execute(
            "SELECT status, resolved_by, resolved_at FROM escalations WHERE id = %s", (eid,)
        ).fetchone()
        assert row["status"] == "resolved" and str(row["resolved_by"]) == operator
        assert row["resolved_at"] is not None
        audit = conn.execute(
            "SELECT action, target_id FROM ops_audit WHERE target_id = %s AND action = 'resolve'",
            (eid,),
        ).fetchone()
    assert audit is not None
    assert sent and sent[0][0] == "support_resolved" and sent[0][1]["support_reference_id"] == eid


def test_resolve_bad_secret_403(_dbpool, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _OP_SECRET)
    from fastapi import HTTPException

    from orchestrator.api.ops_resolve import ResolveEscalationBody, resolve_escalation

    with pytest.raises(HTTPException) as exc:
        resolve_escalation(
            ResolveEscalationBody(escalation_id=str(uuid4()), operator_id="x"),
            x_internal_secret="wrong",
            x_operator_jwt="x",
        )
    assert exc.value.status_code == 403


def test_resolve_idempotent_already_resolved(_dbpool, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("OPERATOR_JWT_SECRET", _OP_SECRET)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_template_message",
        lambda *a, **k: None,
    )
    from orchestrator.api.ops_resolve import ResolveEscalationBody, resolve_escalation

    tid, eid = _seed_escalation(_dbpool)
    operator = str(uuid4())
    resolve_escalation(
        ResolveEscalationBody(escalation_id=eid, operator_id=operator),
        x_internal_secret=_SECRET, x_operator_jwt=_op_jwt(operator),
    )
    out2 = resolve_escalation(
        ResolveEscalationBody(escalation_id=eid, operator_id=operator),
        x_internal_secret=_SECRET, x_operator_jwt=_op_jwt(operator),
    )
    assert out2["status"] == "already_resolved"
