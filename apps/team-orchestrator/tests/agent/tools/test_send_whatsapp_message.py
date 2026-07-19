"""VT-44 — send_whatsapp_message unit tests.

MagicMock pool + injected send_fn; no live DB (mirror test_schedule_followup.py).
CI stdlib-only smoke skips via importorskip("langchain").

Test cases per the approved plan §6:
1.  Happy path — in-window customer → sent
2.  Window expired — last_inbound_at 25h ago → window_closed (window_expired)
3.  No inbound history — last_inbound_at IS NULL → window_closed (no_inbound_history)
4.  Idempotency — same key twice → send_fn called once total
5.  Per-tenant rate limit — 1001st send → rate_limited
6.  Per-customer rate limit — 2nd send within 6h → rate_limited
7.  Cross-tenant — customer not found (RLS) → unauthorized
8.  Twilio 5xx — send_fn raises → status='error', ledger written
9.  PII redaction — phone_e164 never in logger calls / ledger INSERT
10. Body length bounds — empty + >4096 → ValidationError
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# VT-306: _resolve_customer reads via CustomersWrapper.find_by_id on its own
# tenant_connection (not the mock pool's cursor). Patch the wrapper to return the
# row `_pool(customer_row=...)` stages.
_RESOLVED_CUSTOMER: list[Any] = [None]


@pytest.fixture(autouse=True)
def _patch_customer_wrapper(monkeypatch):
    from orchestrator.db.wrappers import CustomersWrapper

    monkeypatch.setattr(
        CustomersWrapper,
        "find_by_id",
        lambda self, tenant_id, row_id, **kw: _RESOLVED_CUSTOMER[0],
    )
    _RESOLVED_CUSTOMER[0] = None
    yield
    _RESOLVED_CUSTOMER[0] = None


def _pool(
    *,
    customer_row: Any = None,
    idem_row: Any = None,
    tenant_count: int = 0,
    customer_count: int = 0,
    raise_on_send_idem_insert: Exception | None = None,
    raise_on_set_tenant: Exception | None = None,
) -> tuple[Any, list[tuple[str, Any]]]:
    """Build a MagicMock pool that hands back controlled query results.

    Returns (pool, executed_calls) where executed_calls is a list of
    (sql_fragment, params) tuples in issue order.
    """
    executed: list[tuple[str, Any]] = []
    cur = MagicMock()
    _RESOLVED_CUSTOMER[0] = customer_row  # VT-306: served by the patched wrapper

    # Response queue: idem check → tenant count → customer count (customer resolve
    # no longer hits this cursor — it's the patched CustomersWrapper.find_by_id).
    responses: list[Any] = [
        idem_row,        # _check_idempotency fetchone
        {"count": tenant_count},   # _check_tenant_rate_limit fetchone
        {"count": customer_count}, # _check_customer_rate_limit fetchone
    ]
    response_idx = [0]

    def _execute(sql: str, params: tuple | None = None) -> None:
        executed.append((sql, params))
        if raise_on_set_tenant and "set_config('app.current_tenant'" in sql:
            raise raise_on_set_tenant
        if raise_on_send_idem_insert and "INSERT INTO send_idempotency_keys" in sql:
            raise raise_on_send_idem_insert

    def _fetchone() -> Any:
        idx = response_idx[0]
        if idx < len(responses):
            response_idx[0] += 1
            return responses[idx]
        return None

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = _fetchone
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, executed


def _customer(
    last_inbound_at: datetime | None,
    phone: str = "+919990000001",
    opt_out_status: str | None = None,
) -> dict:
    return {
        "phone_e164": phone,
        "last_inbound_at": last_inbound_at,
        "opt_out_status": opt_out_status,
    }


def _input(**over: Any):
    from orchestrator.agent.tools.send_whatsapp_message import SendWhatsAppMessageInput

    base = dict(
        tenant_id="tenant-a",
        customer_id="cust-1",
        body="Hello from test",
        idempotency_key="idem-key-1",
    )
    base.update(over)
    return SendWhatsAppMessageInput(**base)  # type: ignore[arg-type]


def _send_fn(body: str, phone: str) -> str:
    return f"SM_test_{phone[-4:]}"


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------

def test_happy_path_sent() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)
    pool, executed = _pool(customer_row=_customer(last_inbound))
    out = send_whatsapp_message(_input(), pool=pool, send_fn=_send_fn)

    assert out.status == "sent"
    assert out.message_sid is not None
    assert "SM_test_" in out.message_sid
    assert out.customer_id == "cust-1"
    assert out.sent_at is not None

    # GUC must be set before any read.
    sql_list = [sql for sql, _ in executed]
    set_idx = next(i for i, s in enumerate(sql_list) if "set_config('app.current_tenant'" in s)
    idem_idx = next(i for i, s in enumerate(sql_list) if "send_idempotency_keys" in s and "SELECT" in s)
    assert set_idx < idem_idx

    # Ledger INSERT must be present.
    assert any("INSERT INTO send_idempotency_keys" in s for s in sql_list)


# ---------------------------------------------------------------------------
# Test 1b (VT-369 Gap-5 PR-1 fix): opted-out customer refused EVEN IN-WINDOW
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["opted_out", "blocked", "owner_excluded"])
def test_opted_out_recipient_refused_even_in_window(status: str) -> None:
    """CL-421/VT-369: an opted-out customer who messages in re-opens a 24h
    *window*, not consent — the freeform path was missing this gate. The refuse
    fires BEFORE any window/rate evaluation and the sender is never called."""
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)  # squarely IN-window
    pool, _ = _pool(customer_row=_customer(last_inbound, opt_out_status=status))
    send_fn = MagicMock(return_value="SM_should_not_be_called")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_opted_out"
    send_fn.assert_not_called()


def test_subscribed_recipient_still_sends_in_window() -> None:
    """The new gate must not over-block: an explicitly 'subscribed' customer
    in-window still sends."""
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)
    pool, _ = _pool(customer_row=_customer(last_inbound, opt_out_status="subscribed"))

    out = send_whatsapp_message(_input(), pool=pool, send_fn=_send_fn)

    assert out.status == "sent"


# ---------------------------------------------------------------------------
# Test 2: Window expired
# ---------------------------------------------------------------------------

def test_window_expired() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=25)
    pool, _ = _pool(customer_row=_customer(last_inbound))
    send_fn = MagicMock(return_value="SM_should_not_be_called")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "window_closed"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "window_expired"
    assert "send_whatsapp_template" in out.error_envelope.message
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: No inbound history (last_inbound_at IS NULL)
# ---------------------------------------------------------------------------

def test_no_inbound_history() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    pool, _ = _pool(customer_row=_customer(None))
    send_fn = MagicMock(return_value="SM_nope")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "window_closed"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "no_inbound_history"
    assert "send_whatsapp_template" in out.error_envelope.message
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Idempotency — same key twice, send_fn called once total
# ---------------------------------------------------------------------------

def test_idempotency_dedup() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)
    send_fn = MagicMock(return_value="SM_first_call")

    # First call: no existing idem row → sends.
    pool1, _ = _pool(customer_row=_customer(last_inbound))
    out1 = send_whatsapp_message(_input(), pool=pool1, send_fn=send_fn)
    assert out1.status == "sent"

    # Second call: idem row already exists (mock returns it).
    existing_idem = {
        "id": "idem-row-1",
        "message_sid": "SM_first_call",
        "send_status": "sent",
        "created_at": _now_utc() - timedelta(minutes=5),
    }
    pool2, _ = _pool(idem_row=existing_idem)
    out2 = send_whatsapp_message(_input(), pool=pool2, send_fn=send_fn)

    assert out2.status == "sent"
    assert out2.message_sid == "SM_first_call"
    # send_fn was only called once across both invocations.
    assert send_fn.call_count == 1


# ---------------------------------------------------------------------------
# Test 4a (VT-410, sibling of VT-387): a cached 'error' row is NOT an
# idempotent hit — RETRYABLE.
#
# A freeform send that TRANSIENTLY failed cached send_status='error' under the
# caller's idempotency_key. With 'error' OUT of _IDEMPOTENT_HIT_STATUSES, a retry
# within the 24h window re-evaluates the gates and SENDS, instead of echoing the
# cached error and silently no-opping.
# ---------------------------------------------------------------------------

def test_errored_row_is_retryable_not_idempotent_hit() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)  # squarely IN-window
    # A prior attempt cached 'error' under this key, 5 min ago (well inside 24h).
    errored_idem = {
        "id": "idem-row-vt410",
        "message_sid": None,
        "send_status": "error",
        "created_at": _now_utc() - timedelta(minutes=5),
    }
    send_fn = MagicMock(return_value="SM_retry_sent")
    pool, executed = _pool(customer_row=_customer(last_inbound), idem_row=errored_idem)

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    # The retry SENDS — the cached 'error' did NOT short-circuit it.
    assert out.status == "sent"
    assert out.message_sid == "SM_retry_sent"
    send_fn.assert_called_once()

    # And the retry wrote a ledger row (ON CONFLICT DO NOTHING is harmless — the
    # key already exists with 'error', but the re-send happened).
    sql_list = [sql for sql, _ in executed]
    assert any("INSERT INTO send_idempotency_keys" in s for s in sql_list)


def test_errored_row_status_not_in_hit_set() -> None:
    """Direct guard on the set itself: 'error' is excluded; the deliverable/terminal
    statuses stay IN so completed sends never re-fire (VT-410)."""
    from orchestrator.agent.tools.send_whatsapp_message import _IDEMPOTENT_HIT_STATUSES

    assert "error" not in _IDEMPOTENT_HIT_STATUSES
    # The dedup contract: a genuinely-delivered send ('sent') and the other
    # non-retryable terminal states STAY hits → never re-processed.
    assert {
        "sent", "window_closed", "rate_limited", "unauthorized",
    } <= _IDEMPOTENT_HIT_STATUSES


# ---------------------------------------------------------------------------
# Test 4b (VT-410 regression guard — LOAD-BEARING): a 'sent' row STAYS an
# idempotent hit. The no-double-send invariant: a freeform message that already
# delivered must NEVER re-send on a retry within the window.
# ---------------------------------------------------------------------------

def test_sent_row_still_dedups_no_resend() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    sent_idem = {
        "id": "idem-row-vt410-sent",
        "message_sid": "SM_already_delivered",
        "send_status": "sent",
        "created_at": _now_utc() - timedelta(minutes=5),
    }
    send_fn = MagicMock(return_value="SM_should_not_fire")
    pool, _ = _pool(idem_row=sent_idem)

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    # Idempotent hit: returns the cached 'sent' WITHOUT calling send_fn again.
    assert out.status == "sent"
    assert out.message_sid == "SM_already_delivered"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Per-tenant rate limit (1001st send)
# ---------------------------------------------------------------------------

def test_per_tenant_rate_limited() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)
    pool, _ = _pool(customer_row=_customer(last_inbound), tenant_count=1001)
    send_fn = MagicMock(return_value="SM_blocked")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "rate_limited"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "tenant_daily_limit"
    assert out.error_envelope.retry_after_ms is not None
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Per-customer rate limit (2nd send within 6h)
# ---------------------------------------------------------------------------

def test_per_customer_rate_limited() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)
    # tenant under limit, customer at limit (count=1 >= 1 allowed)
    pool, _ = _pool(customer_row=_customer(last_inbound), tenant_count=5, customer_count=1)
    send_fn = MagicMock(return_value="SM_blocked")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "rate_limited"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "customer_6h_limit"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: Cross-tenant — customer not visible (RLS)
# ---------------------------------------------------------------------------

def test_cross_tenant_unauthorized() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    # customer_row=None: RLS returned 0 rows (cross-tenant or nonexistent).
    pool, _ = _pool(customer_row=None)
    send_fn = MagicMock(return_value="SM_leaked")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "customer_not_found"
    send_fn.assert_not_called()

    # Critical: no phone number in the output at all.
    output_str = str(out.model_dump())
    assert "+91" not in output_str


# ---------------------------------------------------------------------------
# Test 8: Twilio 5xx — send_fn raises → error envelope, ledger written
# ---------------------------------------------------------------------------

def test_twilio_error_returns_envelope() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    last_inbound = _now_utc() - timedelta(hours=2)
    pool, executed = _pool(customer_row=_customer(last_inbound))

    class FakeTwilioError(Exception):
        pass

    def _fail_send(body: str, phone: str) -> str:
        raise FakeTwilioError("service unavailable")

    out = send_whatsapp_message(_input(), pool=pool, send_fn=_fail_send)

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "twilio_error"
    # Ledger row should have been written with send_status='error'.
    insert_calls = [sql for sql, _ in executed if "INSERT INTO send_idempotency_keys" in sql]
    assert len(insert_calls) == 1


# ---------------------------------------------------------------------------
# Test 9: PII redaction — phone_e164 never in logger output
# ---------------------------------------------------------------------------

def test_pii_not_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    phone = "+919990000099"
    last_inbound = _now_utc() - timedelta(hours=1)
    pool, executed = _pool(customer_row=_customer(last_inbound, phone=phone))

    with caplog.at_level(logging.DEBUG, logger="orchestrator.agent.tools.send_whatsapp_message"):
        send_whatsapp_message(_input(), pool=pool, send_fn=_send_fn)

    # Phone must NOT appear in any log message.
    for record in caplog.records:
        assert phone not in record.getMessage(), (
            f"PII leak: phone found in log message: {record.getMessage()!r}"
        )

    # Phone must NOT appear in the SQL parameters written to the ledger.
    for sql, params in executed:
        if "INSERT INTO send_idempotency_keys" in sql:
            assert phone not in str(params), (
                f"PII leak: phone found in ledger INSERT params: {params!r}"
            )


# ---------------------------------------------------------------------------
# Test 10: Body length bounds — Pydantic ValidationError
# ---------------------------------------------------------------------------

def test_body_empty_raises_validation_error() -> None:
    from pydantic import ValidationError
    from orchestrator.agent.tools.send_whatsapp_message import SendWhatsAppMessageInput

    with pytest.raises(ValidationError):
        SendWhatsAppMessageInput(
            tenant_id="t1",
            customer_id="c1",
            body="",
            idempotency_key="k1",
        )


def test_body_too_long_raises_validation_error() -> None:
    from pydantic import ValidationError
    from orchestrator.agent.tools.send_whatsapp_message import SendWhatsAppMessageInput

    with pytest.raises(ValidationError):
        SendWhatsAppMessageInput(
            tenant_id="t1",
            customer_id="c1",
            body="x" * 4097,
            idempotency_key="k1",
        )


def test_idempotency_key_empty_raises_validation_error() -> None:
    from pydantic import ValidationError
    from orchestrator.agent.tools.send_whatsapp_message import SendWhatsAppMessageInput

    with pytest.raises(ValidationError):
        SendWhatsAppMessageInput(
            tenant_id="t1",
            customer_id="c1",
            body="Hello",
            idempotency_key="",
        )


# ---------------------------------------------------------------------------
# Test: DB error path — never raises
# ---------------------------------------------------------------------------

def test_db_error_returns_envelope_never_raises() -> None:
    from orchestrator.agent.tools.send_whatsapp_message import send_whatsapp_message

    exc = Exception("connection refused")
    pool, _ = _pool(raise_on_set_tenant=exc)
    out = send_whatsapp_message(_input(), pool=pool, send_fn=_send_fn)

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "db_error"
