"""VT-298 — watchdog → assigned-VTR Telegram routing (Rule #15 canary, real Postgres).

Recipient resolution chains operator_assignments (072) → operator_telegram (075), both
deny-all FORCE RLS (service-role only). The watchdog (VT-202 dispatch) must reach the
ASSIGNED VTR's VERIFIED chat — and ONLY that — immediately, IN ADDITION to the OPS chat
(Cowork DECISION 2 = BOTH). Canary tenants stay DEV-bot-only (Cowork canary lock).

No live Telegram: the leaf `send_telegram` is monkeypatched to capture recipients. Real PG
for the routing tables. Gated on DATABASE_URL + dbos. Bot token NOT required (mocked) — flag:
real send needs TELEGRAM_OPS_BOT_TOKEN + each VTR verifying their chat. CL-422 synthetic;
CL-390 no PII in the alert payload (scrubbed upstream).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-298 VTR-telegram routing canary skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


# --- helpers ---------------------------------------------------------------


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-298 test', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])


def _operator(dsn: str) -> str:
    op = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO operator_allowlist (user_id) VALUES (%s)", (op,))
    return op


def _assign(dsn: str, op: str, tenant: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_assignments (operator_id, tenant_id) VALUES (%s, %s)",
            (op, tenant),
        )


def _bind_telegram(dsn: str, op: str, chat_id: str, *, verified: bool = True) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_telegram (operator_id, chat_id, verified_at) "
            "VALUES (%s, %s, %s)",
            (op, chat_id, "now()" if verified else None),
        )


# --- resolver --------------------------------------------------------------


def test_resolve_returns_only_assigned_and_verified(substrate):
    from orchestrator.alerts.vtr_routing import resolve_assigned_vtr_chat_ids

    t, t_other = _tenant(substrate), _tenant(substrate)
    op_ok = _operator(substrate)       # assigned to t + verified → INCLUDED
    op_unverified = _operator(substrate)  # assigned to t but UNVERIFIED → excluded
    op_other = _operator(substrate)    # verified but assigned to t_other → excluded
    _assign(substrate, op_ok, t)
    _bind_telegram(substrate, op_ok, "C-OK", verified=True)
    _assign(substrate, op_unverified, t)
    _bind_telegram(substrate, op_unverified, "C-UNVERIFIED", verified=False)
    _assign(substrate, op_other, t_other)
    _bind_telegram(substrate, op_other, "C-OTHER", verified=True)

    chats = resolve_assigned_vtr_chat_ids(t)
    assert chats == ["C-OK"]


def test_resolve_excludes_revoked_assignment(substrate):
    from orchestrator.alerts.vtr_routing import resolve_assigned_vtr_chat_ids

    t = _tenant(substrate)
    op = _operator(substrate)
    _assign(substrate, op, t)
    _bind_telegram(substrate, op, "C-REVOKED", verified=True)
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "UPDATE operator_assignments SET unassigned_at = now() "
            "WHERE operator_id = %s AND tenant_id = %s",
            (op, t),
        )
    assert resolve_assigned_vtr_chat_ids(t) == []


# --- dispatch fan-out ------------------------------------------------------


def _capture_telegram(monkeypatch):
    """Monkeypatch the dispatch leaf clients; return the captured (chat_id, text) list."""
    captured: list[tuple[str, str]] = []

    async def _fake_send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
        captured.append((chat_id, text))
        return True

    async def _fake_send_email(*_a, **_k) -> bool:
        return True

    from orchestrator.alerts import dispatch as _d

    monkeypatch.setattr(_d, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(_d, "send_resend_email", _fake_send_email)
    return captured


def test_dispatch_fans_out_to_ops_and_assigned_vtr(substrate, monkeypatch):
    from orchestrator.alerts.dispatch import dispatch_alert
    from orchestrator.alerts.triggers import Trigger

    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "ops-token")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "OPS-CHAT")
    monkeypatch.delenv("TEAM_CANARY_TENANT_IDS", raising=False)
    captured = _capture_telegram(monkeypatch)

    t = _tenant(substrate)
    t_other = _tenant(substrate)
    op = _operator(substrate)
    _assign(substrate, op, t)
    _bind_telegram(substrate, op, "C-VTR", verified=True)
    # A VTR on a DIFFERENT tenant — must NOT receive this tenant's alert.
    op_other = _operator(substrate)
    _assign(substrate, op_other, t_other)
    _bind_telegram(substrate, op_other, "C-OTHER-VTR", verified=True)

    trigger = Trigger(
        tenant_id=UUID(t), trigger_kind="hard_limit", severity="critical",
        message_text="agent aborted at hard limit",
    )
    dispatch_alert(trigger)

    chats = {c for c, _ in captured}
    assert "OPS-CHAT" in chats        # OPS chat (existing channel)
    assert "C-VTR" in chats           # assigned VTR (VT-298)
    assert "C-OTHER-VTR" not in chats  # cross-tenant VTR excluded
    # CL-390: payload carries no raw phone digits (scrubbed upstream).
    for _, text in captured:
        assert "agent aborted at hard limit" in text


def test_canary_tenant_dev_only_no_vtr(substrate, monkeypatch):
    from orchestrator.alerts.dispatch import dispatch_alert
    from orchestrator.alerts.triggers import Trigger

    t = _tenant(substrate)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "ops-token")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "OPS-CHAT")
    monkeypatch.setenv("TELEGRAM_DEV_BOT_TOKEN", "dev-token")
    monkeypatch.setenv("TELEGRAM_DEV_CHAT_ID", "DEV-CHAT")
    monkeypatch.setenv("TEAM_CANARY_TENANT_IDS", t)  # this tenant is canary
    captured = _capture_telegram(monkeypatch)

    op = _operator(substrate)
    _assign(substrate, op, t)
    _bind_telegram(substrate, op, "C-CANARY-VTR", verified=True)

    trigger = Trigger(
        tenant_id=UUID(t), trigger_kind="hard_limit", severity="critical",
        message_text="canary crash",
    )
    dispatch_alert(trigger)

    chats = {c for c, _ in captured}
    assert chats == {"DEV-CHAT"}          # DEV only — canary lock
    assert "C-CANARY-VTR" not in chats     # never a real VTR chat
    assert "OPS-CHAT" not in chats
