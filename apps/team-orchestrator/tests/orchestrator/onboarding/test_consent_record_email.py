"""VT-715 — the DPDP consent-record email (pins: renderer facts, pending-without-email,
idempotent stamp, send path, fail-soft)."""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding import consent_record_email as cre  # noqa: E402

_TID = "66666666-6666-6666-6666-666666666666"


def test_renderer_carries_the_record_facts() -> None:
    html = cre.consent_record_html(
        business_name="RKeCom Services", masked_phone="•••5401",
        channel="WhatsApp 'I agree' button", dpdpa_version="v3", residency_version="v2",
        consented_at="2026-07-28T12:00:00Z",
    )
    for frag in ("RKeCom Services", "•••5401", "WhatsApp 'I agree' button", "v3", "v2",
                 "2026-07-28T12:00:00Z", "STOP"):
        assert frag in html


def _wire(monkeypatch, *, row, send_ok=True):
    calls: dict[str, Any] = {"sent": None, "stamped": 0}

    class _Conn:
        def execute(self, sql, params=()):  # noqa: ANN001
            if "UPDATE consent_records" in sql:
                calls["stamped"] += 1

            class _Cur:
                def fetchone(_s):  # noqa: ANN202
                    return row

            return _Cur()

    class _CM:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import orchestrator.db as dbmod

    monkeypatch.setattr(dbmod, "tenant_connection", lambda t: _CM())
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    import orchestrator.alerts.clients as ac

    async def _send(api_key, from_addr, to_addr, subject, html, attachments=None):
        calls["sent"] = {"to": to_addr, "subject": subject}
        return send_ok

    monkeypatch.setattr(ac, "send_resend_email", _send)
    return calls


def test_pending_when_no_owner_email(monkeypatch) -> None:
    calls = _wire(monkeypatch, row=("Biz", "+919999", None, "v3", "v2", "2026-07-28", None))
    assert cre.send_consent_record_email(_TID, channel="WhatsApp 'I agree' button") is False
    assert calls["sent"] is None and calls["stamped"] == 0, "no email → pending, nothing sent"


def test_sends_and_stamps_once(monkeypatch) -> None:
    calls = _wire(monkeypatch, row=("Biz", "+919999", "o@x.in", "v3", "v2", "2026-07-28", None))
    assert cre.send_consent_record_email(_TID, channel="web signup form (UI)") is True
    assert calls["sent"]["to"] == "o@x.in"
    assert calls["stamped"] == 1


def test_never_resends_a_stamped_record(monkeypatch) -> None:
    calls = _wire(monkeypatch, row=("Biz", "+919999", "o@x.in", "v3", "v2", "2026-07-28", "2026-07-28T13:00:00Z"))
    assert cre.send_consent_record_email(_TID, channel="x") is False
    assert calls["sent"] is None


def test_fail_soft_on_db_error(monkeypatch) -> None:
    import orchestrator.db as dbmod

    def _boom(t):
        raise RuntimeError("db down")

    monkeypatch.setattr(dbmod, "tenant_connection", _boom)
    assert cre.send_consent_record_email(_TID, channel="x") is False
