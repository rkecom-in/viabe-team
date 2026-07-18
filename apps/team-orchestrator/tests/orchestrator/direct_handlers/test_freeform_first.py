"""VT-683 P1 — the freeform-first helper: session send wins; the Meta template is only the
transition belt (fires on freeform failure or a missing recipient), always truthfully reported."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("psycopg")

from orchestrator.direct_handlers import _freeform_first as ff  # noqa: E402


class _SendResult:
    def model_dump(self):
        return {
            "success": True, "message_sid": "SMfallback", "error_code": None,
            "error_message": None, "attempted_at": datetime.now(UTC),
            "template_name": "team_x", "recipient_phone_token": "tok",
        }


def test_freeform_success_no_template(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.utils.twilio_send as tw

    calls = []
    monkeypatch.setattr(tw, "send_freeform_message",
                        lambda body, rec, **kw: calls.append(("freeform", body)) or "SMff1")
    monkeypatch.setattr(tw, "send_template_message",
                        lambda *a, **kw: calls.append(("template", a)) or _SendResult())
    out = ff.send_freeform_first("t-1", "hello", "+15550001111", fallback_template="team_x")
    assert out["success"] is True and out["channel"] == "freeform_session"
    assert out["message_sid"] == "SMff1" and out["template_name"] == "team_x"
    assert calls == [("freeform", "hello")]  # the template NEVER fired


def test_freeform_failure_falls_back_to_template(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.utils.twilio_send as tw

    def _boom(*a, **kw):
        raise RuntimeError("63016 window closed")

    monkeypatch.setattr(tw, "send_freeform_message", _boom)
    monkeypatch.setattr(tw, "send_template_message", lambda *a, **kw: _SendResult())
    out = ff.send_freeform_first("t-1", "hello", "+15550001111", fallback_template="team_x")
    assert out["channel"] == "template_fallback" and out["success"] is True
    assert out["message_sid"] == "SMfallback"


def test_no_recipient_goes_straight_to_template(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.utils.twilio_send as tw

    monkeypatch.setattr(tw, "send_freeform_message",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not fire")))
    monkeypatch.setattr(tw, "send_template_message", lambda *a, **kw: _SendResult())
    out = ff.send_freeform_first("t-1", "hello", None, fallback_template="team_x")
    assert out["channel"] == "template_fallback"
