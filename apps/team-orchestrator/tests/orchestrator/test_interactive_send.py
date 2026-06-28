"""VT-479 — send_interactive_message + journey confirm-button presentation.

send_interactive_message sends a pre-created Twilio Content object (HX SID) as an in-session
interactive message via the SAME _client() chokepoint every send uses — so the VT-476 dev send-guard
+ TEAM_TWILIO_MOCK_MODE + the VT-460 customer-send gate all apply unchanged. The journey sends a
CONFIRM question as Yes/No/Skip buttons, falling back to plain freeform text on any failure.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("twilio")
pytest.importorskip("dbos")

from orchestrator.utils import twilio_send  # noqa: E402


class _SpyMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _M:
            sid = "SM" + "0" * 32

        return _M()


class _SpyClient:
    def __init__(self):
        self.messages = _SpyMessages()


def test_interactive_send_funnels_through_client(monkeypatch):
    """send_interactive_message must obtain its transport from _client() (so the VT-476 guard +
    mock-mode apply) and pass content_sid + serialized content_variables."""
    spy = _SpyClient()
    monkeypatch.setattr(twilio_send, "_client", lambda: spy)
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt479-test-salt")

    sid = twilio_send.send_interactive_message(
        "HX60ace8008b02439ca0db444dee6327d2",
        "+919321553267",
        content_variables={"1": "We found you're a Local services business — is that right?"},
    )

    assert sid.startswith("SM")
    assert len(spy.messages.calls) == 1, "interactive send must go through _client().messages.create"
    call = spy.messages.calls[0]
    assert call["content_sid"] == "HX60ace8008b02439ca0db444dee6327d2"
    assert call["to"] == "whatsapp:+919321553267", "recipient must be whatsapp:-prefixed"
    assert call["from_"] == "whatsapp:+910000000000"
    # content_variables serialized to JSON
    assert json.loads(call["content_variables"])["1"].startswith("We found")


def test_interactive_send_no_variables(monkeypatch):
    spy = _SpyClient()
    monkeypatch.setattr(twilio_send, "_client", lambda: spy)
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt479-test-salt")

    twilio_send.send_interactive_message("HXabc", "+919321553267")
    call = spy.messages.calls[0]
    assert "content_variables" not in call, "omit content_variables when none given"


def test_registry_resolves_confirm_buttons_content_sid():
    """The onboarding_confirm_yesno interactive Content object is registered (NO hardcoded SID)."""
    from orchestrator.templates_registry import content_sid_for

    sid = content_sid_for("onboarding_confirm_yesno", "en")
    assert sid and sid.startswith("HX"), "the VT-479 quick-reply Content object must be registered"


def test_journey_send_confirm_uses_buttons(monkeypatch):
    """journey._send for a CONFIRM question sends the interactive buttons (not plain text)."""
    from orchestrator.onboarding import journey

    interactive_calls = []
    freeform_calls = []
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_interactive_message",
        lambda content_sid, recipient, **kw: interactive_calls.append((content_sid, recipient, kw)) or "SMx",
    )
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda body, recipient, **kw: freeform_calls.append((body, recipient)) or "SMy",
    )

    confirm_q = {"field": "city", "kind": "confirm", "prompt_en": "Mumbai — correct?",
                 "prompt_hi": "?", "draft_value": "Mumbai"}
    journey._send("+919321553267", confirm_q, "en")

    assert len(interactive_calls) == 1, "a confirm question must send interactive buttons"
    assert interactive_calls[0][0].startswith("HX")
    assert interactive_calls[0][2]["content_variables"] == {"1": "Mumbai — correct?"}
    assert freeform_calls == [], "no plain-text fallback when buttons succeed"


def test_journey_send_gap_uses_plain_text(monkeypatch):
    """A non-confirm (gap) question stays plain freeform text — no buttons."""
    from orchestrator.onboarding import journey

    interactive_calls = []
    freeform_calls = []
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_interactive_message",
        lambda *a, **k: interactive_calls.append(a) or "SMx",
    )
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda body, recipient, **kw: freeform_calls.append((body, recipient)) or "SMy",
    )

    gap_q = {"field": "operating_hours", "kind": "gap", "prompt_en": "What are your hours?",
             "prompt_hi": "?", "draft_value": None}
    journey._send("+919321553267", gap_q, "en")

    assert interactive_calls == [], "a gap question must NOT use buttons"
    assert len(freeform_calls) == 1, "a gap question is plain freeform text"


def test_journey_send_confirm_falls_back_to_text_on_button_failure(monkeypatch):
    """If the interactive button send raises, the confirm falls back to plain freeform text."""
    from orchestrator.onboarding import journey

    freeform_calls = []

    def _boom(*a, **k):
        raise RuntimeError("twilio interactive send failed")

    monkeypatch.setattr("orchestrator.utils.twilio_send.send_interactive_message", _boom)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.send_freeform_message",
        lambda body, recipient, **kw: freeform_calls.append((body, recipient)) or "SMy",
    )

    confirm_q = {"field": "city", "kind": "confirm", "prompt_en": "Mumbai — correct?",
                 "prompt_hi": "?", "draft_value": "Mumbai"}
    journey._send("+919321553267", confirm_q, "en")

    assert len(freeform_calls) == 1, "button failure must fall back to plain text (journey never breaks)"
    assert freeform_calls[0][0] == "Mumbai — correct?"
