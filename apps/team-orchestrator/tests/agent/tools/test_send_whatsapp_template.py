"""VT-45 — send_whatsapp_template unit tests.

MagicMock pool + injected send_fn; no live DB.
CI stdlib-only smoke skips via importorskip("langchain").

Test matrix per the approved plan §7:
1.  Happy path: known template + valid params + en + resolvable customer -> sent
2.  Unknown template: error_envelope.code='unknown_template', no send
3.  Unsupported language: error_envelope.code='unsupported_language', no send
4.  Missing template params: error_envelope.code='missing_template_params'
5.  Param value too long: error_envelope.code='param_value_invalid', no send
6.  Cross-tenant: customer_id of tenant B -> unauthorized, phone never surfaced
7.  Rate limit: 5001st send -> rate_limited
8.  Idempotency dedupe: same (tenant, key) twice -> send_fn called once
9.  Opted-out recipient: opted_out/blocked -> unauthorized, no send
10. CL-390 PII: phone never in logs
11. Twilio error path: send_fn raises -> error envelope, idempotency ledger written
12. Content variables positional mapping: named params -> {"1":..,"2":..}
13. DB error path: never raises
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain")


# VT-301 / CL-429: the send path now gates on a recorded WhatsApp opt-in
# (consent.has_consent_for_phone), which opens its OWN real tenant_connection —
# the MagicMock pool below never sees it. Default it to True so the mock-pool
# matrix exercises the OTHER behaviours; the opt-in gate itself is asserted
# in test_optin_gate_refuses_unconsented (mock) + the real-PG canary
# (tests/orchestrator/test_send_gate_optin_realdb.py).
@pytest.fixture(autouse=True)
def _grant_consent(monkeypatch):
    from orchestrator.privacy import consent
    monkeypatch.setattr(consent, "has_consent_for_phone", lambda *_a, **_k: True)


# VT-306: _resolve_customer now reads via CustomersWrapper.find_by_id on its own
# tenant_connection (not the mock pool's cursor). Patch the wrapper to return the
# row `_pool(customer_row=...)` stages, so the existing tests keep driving the
# customer-resolution outcome without a live DB.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _pool(
    *,
    customer_row: Any = None,
    idem_row: Any = None,
    tenant_count: int = 0,
    raise_on_set_local: Exception | None = None,
    raise_on_campaign_messages_insert: Exception | None = None,
) -> tuple[Any, list[tuple[str, Any]]]:
    """Build a MagicMock pool that hands back controlled query results.

    Response order (VT-306: customer resolve no longer hits this cursor — it's
    served by the patched CustomersWrapper.find_by_id, set from customer_row):
      1. idem_row (idempotency check SELECT)
      2. tenant_count (rate limit COUNT)
    """
    executed: list[tuple[str, Any]] = []
    cur = MagicMock()
    _RESOLVED_CUSTOMER[0] = customer_row  # served by the patched wrapper

    responses: list[Any] = [
        idem_row,                     # _check_idempotency fetchone
        {"count": tenant_count},      # _check_tenant_rate_limit fetchone
    ]
    response_idx = [0]

    def _execute(sql: str, params: tuple | None = None) -> None:
        executed.append((sql, params))
        # VT-140 fix: the GUC is set via set_config('app.current_tenant', ...)
        # (parameterizable) — NOT "SET LOCAL ... = %s" (a Postgres syntax error).
        # Match on the GUC name so this stays valid across the corrected SQL.
        if raise_on_set_local and "app.current_tenant" in sql:
            raise raise_on_set_local
        if raise_on_campaign_messages_insert and "INSERT INTO campaign_messages" in sql:
            raise raise_on_campaign_messages_insert

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
    phone: str = "+919990000001",
    opt_out_status: str | None = None,
    complaint_status: str | None = None,
) -> dict:
    return {
        "phone_e164": phone,
        "opt_out_status": opt_out_status,
        "complaint_status": complaint_status,
    }


def _input(**over: Any):
    from orchestrator.agent.tools.send_whatsapp_template import SendWhatsappTemplateInput

    base = dict(
        tenant_id="a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        customer_id="b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a12",
        template_id="team_weekly_approval",
        language="en",
        template_params={
            "customer_segment": "SMB",
            "campaign_mode": "recovery",
            "projected_recovery_inr": "5000",
        },
        idempotency_key="idem-key-vt45-test",
    )
    base.update(over)
    return SendWhatsappTemplateInput(**base)  # type: ignore[arg-type]


class _FakeSendResult:
    """Minimal SendResult shape from twilio_send.send_template_message."""
    def __init__(
        self, *, success: bool, message_sid: str | None = None,
        error_code: str | None = None, error_message: str | None = None,
    ) -> None:
        self.success = success
        self.message_sid = message_sid
        self.error_code = error_code
        self.error_message = error_message


def _ok_send_fn(*_args: Any, **_kwargs: Any) -> _FakeSendResult:
    return _FakeSendResult(success=True, message_sid="MK" + "a" * 30)


def _fail_send_fn(*_args: Any, **_kwargs: Any) -> _FakeSendResult:
    return _FakeSendResult(
        success=False, error_code="30008", error_message="Unknown error",
    )


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------

def test_happy_path_sent() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, executed = _pool(customer_row=_customer())
    out = send_whatsapp_template(_input(), pool=pool, send_fn=_ok_send_fn)

    assert out.status == "sent"
    assert out.message_sid is not None
    assert out.customer_id is not None
    assert out.sent_at is not None
    assert out.error_envelope is None

    sql_list = [sql for sql, _ in executed]
    # GUC before any query. VT-140 fix: set via set_config('app.current_tenant',
    # ...) — match on the GUC name (the corrected SQL is set_config, not SET
    # LOCAL, which cannot bind a parameter).
    set_idx = next(i for i, s in enumerate(sql_list) if "app.current_tenant" in s)
    idem_idx = next(i for i, s in enumerate(sql_list) if "send_idempotency_keys" in s and "SELECT" in s)
    assert set_idx < idem_idx

    # Ledger INSERT present.
    assert any("INSERT INTO send_idempotency_keys" in s for s in sql_list)
    # campaign_messages INSERT present.
    assert any("INSERT INTO campaign_messages" in s for s in sql_list)


# ---------------------------------------------------------------------------
# Test 2: Unknown template
# ---------------------------------------------------------------------------

def test_unknown_template() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    send_fn = MagicMock()
    pool, _ = _pool()
    out = send_whatsapp_template(
        _input(template_id="team_does_not_exist"),
        pool=pool,
        send_fn=send_fn,
    )

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "unknown_template"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Unsupported language
# ---------------------------------------------------------------------------

def test_unsupported_language(monkeypatch) -> None:
    from orchestrator import templates_registry
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    # Both input languages (en, hi) are now configured for all 8 templates
    # (hi populated by VT-163-fix-1), so an unconfigured variant is no longer
    # reachable via the en|hi Literal input. Simulate the registry raising
    # UnknownLanguageVariantError to exercise the tool's catch → error envelope.
    def _raise(name, lang, *a, **k):
        raise templates_registry.UnknownLanguageVariantError(name, lang)

    monkeypatch.setattr(templates_registry, "resolve", _raise)
    send_fn = MagicMock()
    pool, _ = _pool()
    out = send_whatsapp_template(
        _input(language="hi"),
        pool=pool,
        send_fn=send_fn,
    )

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "unsupported_language"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Missing template params
# ---------------------------------------------------------------------------

def test_missing_template_params() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    send_fn = MagicMock()
    pool, _ = _pool()
    # Omit "projected_recovery_inr".
    out = send_whatsapp_template(
        _input(template_params={
            "customer_segment": "SMB",
            "campaign_mode": "recovery",
            # projected_recovery_inr missing
        }),
        pool=pool,
        send_fn=send_fn,
    )

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "missing_template_params"
    assert "projected_recovery_inr" in out.error_envelope.message
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Param value too long
# ---------------------------------------------------------------------------

def test_param_value_too_long() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    send_fn = MagicMock()
    pool, _ = _pool()
    out = send_whatsapp_template(
        _input(template_params={
            "customer_segment": "x" * 1025,  # exceeds 1024
            "campaign_mode": "recovery",
            "projected_recovery_inr": "5000",
        }),
        pool=pool,
        send_fn=send_fn,
    )

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "param_value_invalid"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Cross-tenant unauthorized
# ---------------------------------------------------------------------------

def test_cross_tenant_unauthorized() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    # customer_row=None: RLS returned 0 rows (cross-tenant or nonexistent).
    pool, _ = _pool(customer_row=None)
    send_fn = MagicMock()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "customer_not_found"
    send_fn.assert_not_called()

    # Critical: no phone number in output.
    output_str = str(out.model_dump())
    assert "+91" not in output_str


# ---------------------------------------------------------------------------
# Test 7: Rate limit (5001st send)
# ---------------------------------------------------------------------------

def test_per_tenant_rate_limited() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, _ = _pool(customer_row=_customer(), tenant_count=5001)
    send_fn = MagicMock()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "rate_limited"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "tenant_daily_limit"
    assert out.error_envelope.retry_after_ms is not None
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: Idempotency dedupe
# ---------------------------------------------------------------------------

def test_idempotency_dedup() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    send_fn = MagicMock(side_effect=_ok_send_fn)

    # First call: no existing idem row -> sends.
    pool1, _ = _pool(customer_row=_customer())
    out1 = send_whatsapp_template(_input(), pool=pool1, send_fn=send_fn)
    assert out1.status == "sent"

    # Second call: idem row already exists (mock returns it).
    existing_idem = {
        "id": "idem-row-vt45",
        "message_sid": "MK" + "b" * 30,
        "send_status": "sent",
        "created_at": _now_utc() - timedelta(minutes=5),
    }
    pool2, _ = _pool(idem_row=existing_idem)
    out2 = send_whatsapp_template(_input(), pool=pool2, send_fn=send_fn)

    assert out2.status == "sent"
    assert out2.message_sid == "MK" + "b" * 30
    # send_fn was called exactly once (first call only).
    assert send_fn.call_count == 1


# ---------------------------------------------------------------------------
# Test 8a (VT-387): a cached 'error' row is NOT an idempotent hit — RETRYABLE.
#
# The money-adjacent fix: a draft whose send TRANSIENTLY failed cached
# send_status='error' under the fixed key agent:{draft_id}. With 'error' OUT of
# _IDEMPOTENT_HIT_STATUSES, a retry within the 24h window re-evaluates the gates
# and SENDS, instead of echoing the cached error and silently no-opping.
# ---------------------------------------------------------------------------

def test_errored_row_is_retryable_not_idempotent_hit() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    # A prior attempt cached 'error' under this key, 5 min ago (well inside 24h).
    errored_idem = {
        "id": "idem-row-vt387",
        "message_sid": None,
        "send_status": "error",
        "created_at": _now_utc() - timedelta(minutes=5),
    }
    send_fn = MagicMock(side_effect=_ok_send_fn)
    pool, executed = _pool(customer_row=_customer(), idem_row=errored_idem)

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    # The retry SENDS — the cached 'error' did NOT short-circuit it.
    assert out.status == "sent"
    assert out.message_sid is not None
    send_fn.assert_called_once()

    # And the retry wrote a 'sent' ledger row (ON CONFLICT DO NOTHING is harmless
    # — the key already exists with 'error', but the re-send happened).
    sql_list = [sql for sql, _ in executed]
    assert any("INSERT INTO send_idempotency_keys" in s for s in sql_list)


def test_errored_row_status_not_in_hit_set() -> None:
    """Direct guard on the set itself: 'error' is excluded; the deliverable/terminal
    statuses stay IN so completed sends never re-fire (VT-387)."""
    from orchestrator.agent.tools.send_whatsapp_template import (
        _IDEMPOTENT_HIT_STATUSES,
    )

    assert "error" not in _IDEMPOTENT_HIT_STATUSES
    # The dedup contract: a genuinely-delivered send ('sent') and the other
    # non-retryable terminal states STAY hits → never re-processed.
    assert {"sent", "dry_run", "rate_limited", "unauthorized"} <= _IDEMPOTENT_HIT_STATUSES


# ---------------------------------------------------------------------------
# Test 8b (VT-387 regression guard — LOAD-BEARING): a 'sent' row STAYS an
# idempotent hit. The no-double-send invariant: a draft that already delivered
# must NEVER re-send on a retry within the window.
# ---------------------------------------------------------------------------

def test_sent_row_still_dedups_no_resend() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    sent_idem = {
        "id": "idem-row-vt387-sent",
        "message_sid": "MK" + "c" * 30,
        "send_status": "sent",
        "created_at": _now_utc() - timedelta(minutes=5),
    }
    send_fn = MagicMock(side_effect=_ok_send_fn)
    pool, _ = _pool(idem_row=sent_idem)

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    # Idempotent hit: returns the cached 'sent' WITHOUT calling send_fn again.
    assert out.status == "sent"
    assert out.message_sid == "MK" + "c" * 30
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: Opted-out/blocked recipient
# ---------------------------------------------------------------------------

def test_opted_out_recipient_refused() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, _ = _pool(customer_row=_customer(opt_out_status="opted_out"))
    send_fn = MagicMock()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_opted_out"
    send_fn.assert_not_called()


def test_blocked_recipient_refused() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, _ = _pool(customer_row=_customer(opt_out_status="blocked"))
    send_fn = MagicMock()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_opted_out"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9a (VT-369 Gap-5 PR-1 adjacent fix): open complaint -> refused
# ---------------------------------------------------------------------------

def test_complaint_open_recipient_refused() -> None:
    """VT-321/VT-369: a customer with complaint_status='open' is hard-refused at
    the tool boundary (mirrors the opt-out refuse — the campaign-execute freeze
    alone left the direct-tool path open)."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, _ = _pool(
        customer_row=_customer(opt_out_status="subscribed", complaint_status="open")
    )
    send_fn = MagicMock()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_complaint_open"
    send_fn.assert_not_called()


def test_complaint_resolved_recipient_passes_gate() -> None:
    """A RESOLVED complaint is not a freeze — the send proceeds."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, _ = _pool(
        customer_row=_customer(opt_out_status="subscribed", complaint_status="resolved")
    )

    out = send_whatsapp_template(_input(), pool=pool, send_fn=_ok_send_fn)

    assert out.status == "sent"


# ---------------------------------------------------------------------------
# Test 9b: VT-301 / CL-429 opt-in gate — no recorded opt-in -> refused
# ---------------------------------------------------------------------------

def test_optin_gate_refuses_unconsented(monkeypatch) -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template
    from orchestrator.privacy import consent

    # Override the autouse grant: this customer has NO opt-in on record.
    monkeypatch.setattr(consent, "has_consent_for_phone", lambda *_a, **_k: False)

    # opt_out_status='subscribed' clears the opt-out check — the gate is what
    # refuses (proving the gate is independent of opt_out_status, applies to
    # owner-entered customers too: owner_inputs is not a WhatsApp opt-in).
    pool, _ = _pool(customer_row=_customer(opt_out_status="subscribed"))
    send_fn = MagicMock()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_not_opted_in"
    send_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 10: CL-390 PII — phone never in logs or ledger
# ---------------------------------------------------------------------------

def test_pii_not_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    phone = "+919990000099"
    pool, executed = _pool(customer_row=_customer(phone=phone))

    with caplog.at_level(
        logging.DEBUG, logger="orchestrator.agent.tools.send_whatsapp_template",
    ):
        send_whatsapp_template(_input(), pool=pool, send_fn=_ok_send_fn)

    for record in caplog.records:
        assert phone not in record.getMessage(), (
            f"PII leak: phone found in log: {record.getMessage()!r}"
        )

    for sql, params in executed:
        if "INSERT INTO send_idempotency_keys" in sql:
            assert phone not in str(params), (
                f"PII leak: phone in ledger INSERT params: {params!r}"
            )


# ---------------------------------------------------------------------------
# Test 11: Twilio error path
# ---------------------------------------------------------------------------

def test_twilio_error_returns_envelope() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, executed = _pool(customer_row=_customer())

    class FakeTwilioError(Exception):
        pass

    def _raise_send(*args: Any, **kwargs: Any) -> None:
        raise FakeTwilioError("service unavailable")

    out = send_whatsapp_template(_input(), pool=pool, send_fn=_raise_send)

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "twilio_error"

    # VT-420: TWO send_idempotency_keys writes now — the pre-send 'sending' in-flight
    # marker (committed BEFORE the Twilio call) + the error flip that upserts it to
    # 'error' afterward (a raise means Twilio did NOT accept → retryable, not 'sent').
    sql_list = [sql for sql, _ in executed]
    insert_calls = [s for s in sql_list if "INSERT INTO send_idempotency_keys" in s]
    assert len(insert_calls) == 2
    # First write is the in-flight 'sending' marker; the second is the 'error' upsert.
    assert "'sending'" in insert_calls[0]
    assert "ON CONFLICT (tenant_id, idempotency_key) DO UPDATE" in insert_calls[1]
    error_params = next(
        params for sql, params in executed
        if "INSERT INTO send_idempotency_keys" in sql and "DO UPDATE" in sql
    )
    assert "error" in error_params


def test_twilio_failure_result_envelope() -> None:
    """send_fn returns success=False (Twilio 4xx) -> error envelope."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    pool, _ = _pool(customer_row=_customer())
    out = send_whatsapp_template(_input(), pool=pool, send_fn=_fail_send_fn)

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "30008"


# ---------------------------------------------------------------------------
# Test 12: Positional content_variables mapping
# ---------------------------------------------------------------------------

def test_content_variables_positional_mapping() -> None:
    """Named params are mapped to {"1":v1, "2":v2, "3":v3} by registry order."""
    from orchestrator.agent.tools.send_whatsapp_template import (
        _build_content_variables,
    )

    # team_weekly_approval: variables = [customer_segment, campaign_mode, projected_recovery_inr]
    variables: tuple[str, ...] = (
        "customer_segment", "campaign_mode", "projected_recovery_inr",
    )
    params = {
        "customer_segment": "SMB",
        "campaign_mode": "recovery",
        "projected_recovery_inr": "5000",
    }
    result = _build_content_variables(variables, params)

    assert result == {"1": "SMB", "2": "recovery", "3": "5000"}


def test_content_variables_reproducible() -> None:
    """Same input -> same mapping twice (reproducibility gate)."""
    from orchestrator.agent.tools.send_whatsapp_template import _build_content_variables

    variables: tuple[str, ...] = (
        "customer_segment", "campaign_mode", "projected_recovery_inr",
    )
    params = {
        "customer_segment": "Retail",
        "campaign_mode": "upsell",
        "projected_recovery_inr": "12000",
    }
    r1 = _build_content_variables(variables, params)
    r2 = _build_content_variables(variables, params)
    assert r1 == r2


# ---------------------------------------------------------------------------
# Test 13: DB error path — never raises
# ---------------------------------------------------------------------------

def test_db_error_returns_envelope_never_raises() -> None:
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    exc = Exception("connection refused")
    pool, _ = _pool(raise_on_set_local=exc)
    out = send_whatsapp_template(_input(), pool=pool, send_fn=_ok_send_fn)

    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "db_error"


# ---------------------------------------------------------------------------
# Test: Pydantic validation on input model
# ---------------------------------------------------------------------------

def test_empty_idempotency_key_raises() -> None:
    from pydantic import ValidationError
    from orchestrator.agent.tools.send_whatsapp_template import SendWhatsappTemplateInput

    with pytest.raises(ValidationError):
        SendWhatsappTemplateInput(
            tenant_id="t1",
            customer_id="c1",
            template_id="team_weekly_approval",
            language="en",
            template_params={},
            idempotency_key="",
        )


def test_invalid_language_raises() -> None:
    from pydantic import ValidationError
    from orchestrator.agent.tools.send_whatsapp_template import SendWhatsappTemplateInput

    with pytest.raises(ValidationError):
        SendWhatsappTemplateInput(
            tenant_id="t1",
            customer_id="c1",
            template_id="team_weekly_approval",
            language="fr",  # type: ignore[arg-type]
            template_params={},
            idempotency_key="k1",
        )


# ===========================================================================
# VT-420 — crash-window canary (Rule #15, money-send LOAD-BEARING)
#
# The bug: send_whatsapp_template's Twilio messages.create call and the autocommit
# 'sent' ledger INSERT are NOT one transaction. A crash AFTER Twilio dispatch but
# BEFORE the 'sent' commit left the key absent → on recovery the send re-fired
# (double-charge / double-message). Twilio's Messages/Content API has no native
# idempotency key (twilio 9.10.9 messages.create exposes none — proven directly
# below), so the fix is a pre-send 'sending' (in-flight) marker, committed BEFORE
# the Twilio call: on recovery a still-'sending' key blocks the re-send fail-SAFE.
#
# The canary simulates the crash window over a DURABLE ledger (modelling the
# autocommit table that survives the process death) and asserts: a recovery attempt
# makes NO second Twilio call. Shared by L2 + L3 (both funnel through this tool).
# ===========================================================================


class _DurableLedgerPool:
    """A MagicMock-free fake pool whose send_idempotency_keys rows PERSIST across
    send_whatsapp_template calls — modelling the autocommit table that survives a
    process crash. Honours the in-flight marker INSERT (ON CONFLICT DO NOTHING), the
    terminal upsert (ON CONFLICT DO UPDATE WHERE send_status <> 'sent'), the
    idempotency SELECT, and the rate-limit COUNT. Everything else (set_config,
    campaign_messages) is accepted as a no-op.

    ``crash_after_send`` — if set, the cursor raises CrashSimulated the FIRST time a
    terminal upsert (DO UPDATE) is attempted, i.e. AFTER the Twilio call succeeded but
    BEFORE the 'sent' row commits: the exact crash window. The in-flight 'sending' row
    written before the Twilio call has ALREADY been committed (it persists in
    ``self.rows``), so the second attempt sees it.
    """

    class CrashSimulated(BaseException):
        # BaseException (not Exception) so send_whatsapp_template's broad
        # `except Exception` does NOT catch it — this models an ABRUPT process death
        # (SIGKILL / OOM / deploy restart) in the crash window, the real-world failure
        # mode, not a catchable in-flow error. The 'sent' upsert never commits; the
        # already-committed 'sending' marker is all that survives.
        pass

    def __init__(self, *, crash_after_send: bool = False) -> None:
        # key: (tenant_id, idempotency_key) -> row dict
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._crash_armed = crash_after_send
        self.sql_log: list[tuple[str, Any]] = []

    # --- the cursor protocol send_whatsapp_template uses -----------------
    def _execute(self, sql: str, params: tuple | None = None) -> None:
        self.sql_log.append((sql, params))
        s = " ".join(sql.split())  # normalise whitespace for matching
        if "INSERT INTO send_idempotency_keys" in s and "VALUES (%s, %s, %s, NULL, 'sending')" in s:
            # In-flight marker: ON CONFLICT DO NOTHING. Commits instantly (autocommit).
            tid, key, cid = params  # type: ignore[misc]
            self.rows.setdefault(
                (tid, key),
                {"id": f"row-{key}", "message_sid": None,
                 "send_status": "sending", "created_at": _now_utc()},
            )
            self._last_select = None
            return
        if "INSERT INTO send_idempotency_keys" in s and "DO UPDATE" in s:
            # Terminal upsert ('sent' or 'error'). THIS is the post-Twilio commit.
            if self._crash_armed:
                self._crash_armed = False
                raise _DurableLedgerPool.CrashSimulated("process died before 'sent' commit")
            tid, key, cid, sid, status = params  # type: ignore[misc]
            row = self.rows.get((tid, key))
            if row is None:
                self.rows[(tid, key)] = {
                    "id": f"row-{key}", "message_sid": sid,
                    "send_status": status, "created_at": _now_utc(),
                }
            elif row["send_status"] != "sent":  # WHERE send_status <> 'sent'
                row["send_status"] = status
                row["message_sid"] = sid
            self._last_select = None
            return
        if "send_idempotency_keys" in s and "SELECT" in s and "COUNT" not in s.upper():
            # Idempotency check SELECT.
            tid, key = params  # type: ignore[misc]
            self._last_select = self.rows.get((tid, key))
            return
        if "COUNT(*)" in s.upper() and "send_idempotency_keys" in s:
            self._last_select = {"count": 0}  # never rate-limited in the canary
            return
        # set_config, campaign_messages INSERT, anything else: no-op.
        self._last_select = None

    def _fetchone(self) -> Any:
        return getattr(self, "_last_select", None)

    # --- context-manager plumbing matching pool.connection()/conn.cursor() ---
    # NOTE: `with` looks up __enter__/__exit__ on the TYPE, not the instance, so
    # these must be real classes (not SimpleNamespace with instance dunders).
    def cursor(self) -> Any:
        pool = self

        class _Cur:
            execute = staticmethod(pool._execute)
            fetchone = staticmethod(pool._fetchone)

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_a: Any) -> bool:
                return False

        return _Cur()

    def connection(self) -> Any:
        pool = self

        class _Conn:
            cursor = staticmethod(pool.cursor)

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_a: Any) -> bool:
                return False

        return _Conn()


def _spy_send_fn() -> Any:
    """A transport spy: counts Twilio calls, returns a successful SendResult. No
    network. Each call is a distinct message_sid so a double-send is detectable."""
    calls = {"n": 0}

    def _send(*_args: Any, **_kwargs: Any) -> _FakeSendResult:
        calls["n"] += 1
        return _FakeSendResult(success=True, message_sid=f"MK{'s' * 28}{calls['n']:02d}")

    _send.calls = calls  # type: ignore[attr-defined]
    return _send


def test_vt420_twilio_sdk_has_no_idempotency_key() -> None:
    """The premise of the fix: twilio 9.x messages.create exposes NO idempotency-key
    parameter (so we cannot delegate dedup to Twilio — the pre-send marker is required).
    Skips cleanly if the SDK is absent (dep-less smoke)."""
    twilio_rest = pytest.importorskip("twilio.rest.api.v2010.account.message")
    import inspect

    params = list(inspect.signature(twilio_rest.MessageList.create).parameters)
    idempotency_params = [p for p in params if "idempot" in p.lower()]
    assert idempotency_params == [], (
        f"twilio messages.create unexpectedly exposes idempotency params {idempotency_params}; "
        "re-evaluate VT-420 — the native-key path may now be available"
    )


def test_vt420_crash_window_no_double_send() -> None:
    """THE canary. Attempt 1: Twilio succeeds, then the process 'crashes' BEFORE the
    'sent' commit (the in-flight 'sending' marker is already durable). Attempt 2
    (recovery): MUST NOT call Twilio again — the 'sending' marker blocks the re-send."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    _RESOLVED_CUSTOMER[0] = _customer()
    pool = _DurableLedgerPool(crash_after_send=True)
    send_fn = _spy_send_fn()

    # --- Attempt 1: crashes in the window (Twilio sent, 'sent' commit raises) ---
    with pytest.raises(_DurableLedgerPool.CrashSimulated):
        send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)
    assert send_fn.calls["n"] == 1, "attempt 1 must have dispatched to Twilio exactly once"
    # The durable in-flight marker survived the crash.
    key = ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "idem-key-vt45-test")
    assert pool.rows[key]["send_status"] == "sending"

    # --- Attempt 2: recovery. A fresh pool.connection() but the SAME durable rows. ---
    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    # THE money-safety assertion: NO second Twilio call.
    assert send_fn.calls["n"] == 1, (
        f"DOUBLE-SEND: recovery re-dispatched to Twilio "
        f"(call_count={send_fn.calls['n']}, expected 1)"
    )
    # Recovery reports a terminal 'sent' (probably-already-delivered) with no SID —
    # the caller marks the draft terminal and stops retrying (no double-charge).
    assert out.status == "sent"
    assert out.message_sid is None


def test_vt420_inflight_marker_written_before_twilio_call() -> None:
    """Ordering proof: the 'sending' marker INSERT is committed BEFORE messages.create.
    If it were written after, the crash window would not be closed."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    _RESOLVED_CUSTOMER[0] = _customer()
    pool = _DurableLedgerPool()
    order: list[str] = []

    def _send(*_a: Any, **_k: Any) -> _FakeSendResult:
        order.append("twilio_send")
        return _FakeSendResult(success=True, message_sid="MK" + "z" * 30)

    # Tag the in-flight marker write in the SQL log via a wrapper.
    orig_execute = pool._execute

    def _tap(sql: str, params: tuple | None = None) -> None:
        if "VALUES (%s, %s, %s, NULL, 'sending')" in " ".join(sql.split()):
            order.append("inflight_marker")
        orig_execute(sql, params)

    pool._execute = _tap  # type: ignore[method-assign]

    out = send_whatsapp_template(_input(), pool=pool, send_fn=_send)
    assert out.status == "sent"
    assert order == ["inflight_marker", "twilio_send"], (
        f"in-flight marker must precede the Twilio call; got {order}"
    )


def test_vt420_happy_path_sends_once_marker_flips_to_sent() -> None:
    """Happy path unbroken: exactly ONE Twilio call, and the 'sending' marker is
    upserted to terminal 'sent' (+ the real SID)."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    _RESOLVED_CUSTOMER[0] = _customer()
    pool = _DurableLedgerPool()
    send_fn = _spy_send_fn()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)

    assert out.status == "sent"
    assert send_fn.calls["n"] == 1
    key = ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "idem-key-vt45-test")
    assert pool.rows[key]["send_status"] == "sent"
    assert pool.rows[key]["message_sid"] is not None

    # A clean re-run (no crash) is also an idempotent hit — never re-sends.
    out2 = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)
    assert out2.status == "sent"
    assert send_fn.calls["n"] == 1, "a completed 'sent' send must never re-fire"


def test_vt420_twilio_reject_flips_marker_to_error_retryable() -> None:
    """A Twilio 4xx reject (success=False) means NO message was dispatched → the
    'sending' marker must flip to 'error' (retryable per VT-387), NOT stay 'sending'
    (which would wrongly block a legitimate retry) and NOT become 'sent'."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    _RESOLVED_CUSTOMER[0] = _customer()
    pool = _DurableLedgerPool()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=_fail_send_fn)
    assert out.status == "error"
    key = ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "idem-key-vt45-test")
    assert pool.rows[key]["send_status"] == "error", (
        "a Twilio reject must leave the key retryable ('error'), not block it ('sending')"
    )

    # Recovery after a reject DOES re-send (the message never went out) — 'error' is
    # not an idempotent hit (VT-387), and the marker no longer says 'sending'.
    send_fn = _spy_send_fn()
    out2 = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)
    assert out2.status == "sent"
    assert send_fn.calls["n"] == 1


def test_vt420_stop_still_kills_sends() -> None:
    """The STOP gate (opt-out hard-refuse) is unaffected by the marker change: an
    opted-out recipient is refused with NO Twilio call AND no 'sending' marker written
    (the gate runs before the in-flight marker)."""
    from orchestrator.agent.tools.send_whatsapp_template import send_whatsapp_template

    _RESOLVED_CUSTOMER[0] = _customer(opt_out_status="opted_out")
    pool = _DurableLedgerPool()
    send_fn = _spy_send_fn()

    out = send_whatsapp_template(_input(), pool=pool, send_fn=send_fn)
    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_opted_out"
    assert send_fn.calls["n"] == 0, "STOP must kill the send — no Twilio call"
    # No in-flight marker was written (the gate short-circuits before it).
    key = ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "idem-key-vt45-test")
    assert key not in pool.rows
