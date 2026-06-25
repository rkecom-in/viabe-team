"""VT-426 (Row C) — the trial-sweep owner-notify seam (``trial_sweep._owner_notify``).

These are pure-seam tests: NO DB pool, NO real Twilio. The recipient read
(``get_tenant_whatsapp_number``) is monkeypatched and the send is injected via
``send_fn`` (a recorder), so the whole file is dep-less-safe and runs with 0 external
calls. The point under test is the WIRING:

  - registered + approved template → notify resolves it by NAME from the registry,
    resolves the owner recipient, and sends ONCE with the owner's preferred-language
    variant + the conversion-link params;
  - UNREGISTERED template (the current NEEDS-FAZAL reality for the owner trial-ending
    SID) → FAIL-SAFE SKIP with a loud log, 0 sends, no crash;
  - pending-approval stub SID (content_sid=None) → same fail-safe skip;
  - no reachable recipient → skip;
  - a raising / success=False send → swallowed loudly, the sweep never aborts.
"""

from __future__ import annotations

import logging
import uuid


class _FakeSendResult:
    """Stand-in for utils.twilio_send.SendResult — only the fields _owner_notify reads."""

    def __init__(self, *, success: bool, error_code: str | None = None) -> None:
        self.success = success
        self.error_code = error_code


def test_owner_notify_sends_registered_template_with_language(monkeypatch):
    """A REGISTERED template (real SID in the registry) → notify resolves it by NAME,
    resolves the owner recipient, and invokes the send_fn ONCE with the tenant's
    preferred-language variant + the params (the conversion link). 0 real Twilio."""
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    calls: list[tuple] = []

    def _recording_send(t, template_name, language, params, *, recipient_phone):
        calls.append((str(t), template_name, language, params, recipient_phone))
        return _FakeSendResult(success=True)

    # _owner_notify lazy-imports get_tenant_whatsapp_number FROM twilio_send at call
    # time, so patching the source attribute is what the function picks up.
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: "+919812345678",
    )

    link = "https://viabe.ai/team/subscribe?t=tok"
    # trial_subscribe_link is a REGISTERED template (real SID, owner audience) — resolves
    # past the fail-safe, so the recorded send_fn is invoked. 0 real Twilio (injected fn).
    ts._owner_notify(
        tid,
        "trial_subscribe_link",
        "hi",
        {"owner_name": "Raj Cafe", "subscribe_link": link},
        send_fn=_recording_send,
    )

    assert len(calls) == 1
    sent_tid, tpl, lang, params, recipient = calls[0]
    assert sent_tid == str(tid)
    assert tpl == "trial_subscribe_link"
    assert lang == "hi"  # the owner's preferred-language variant, not hardcoded 'en'
    assert params["subscribe_link"] == link
    assert recipient == "+919812345678"


def test_owner_notify_skips_unregistered_template_loudly(monkeypatch, caplog):
    """The CURRENT reality for the owner trial-ending SID is NEEDS-FAZAL: an UNREGISTERED
    template name → _owner_notify FAIL-SAFE SKIPS with a loud log, makes 0 sends, and
    never even resolves a recipient or crashes."""
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    def _should_not_send(*a, **k):
        sends.append((a, k))
        return _FakeSendResult(success=True)

    def _recipient_must_not_be_read(t):
        raise AssertionError("recipient resolved before the unregistered-template skip")

    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        _recipient_must_not_be_read,
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_ending_NOT_IN_REGISTRY", "en", {"x": "y"},
            send_fn=_should_not_send,
        )

    assert sends == []  # 0 sends — a broken/absent template is never sent
    assert any(
        "SKIP owner-notify" in r.message and "UNREGISTERED" in r.message
        for r in caplog.records
    ), "expected a loud SKIP/UNREGISTERED warning"


def test_owner_notify_skips_pending_stub_sid(monkeypatch, caplog):
    """A registered template whose SID is a pending-approval stub (content_sid=None) →
    FAIL-SAFE SKIP, 0 sends. This is the exact fail-safe the owner trial-ending SID rides
    until Fazal provisions the approved SID into the registry."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    pending = tr.TemplateEntry(
        template_name="trial_ending",
        language="en",
        content_sid=None,  # pending Meta approval
        audience="owner",
        variables=("owner_name", "trial_end_date"),
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: pending)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before stub skip")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_ending", "en",
            {"owner_name": "Raj Cafe", "trial_end_date": "2026-07-14"},
            send_fn=lambda *a, **k: sends.append((a, k)),
        )

    assert sends == []
    assert any(
        "SKIP owner-notify" in r.message and "pending-approval stub" in r.message
        for r in caplog.records
    ), "expected a loud SKIP/pending-stub warning"


def test_owner_notify_no_recipient_skips(monkeypatch, caplog):
    """Registered + approved template but the tenant has no reachable whatsapp_number →
    skip with a loud log, 0 sends, no crash."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    approved = tr.TemplateEntry(
        template_name="trial_subscribe_link", language="en",
        content_sid="HX" + "0" * 32, audience="owner",
        variables=("owner_name", "subscribe_link"),
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: approved)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number", lambda t: None
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_subscribe_link", "en",
            {"owner_name": "x", "subscribe_link": "y"},
            send_fn=lambda *a, **k: sends.append((a, k)),
        )

    assert sends == []
    assert any(
        "SKIP owner-notify" in r.message and "whatsapp_number" in r.message
        for r in caplog.records
    )


def test_owner_notify_send_failure_does_not_crash(monkeypatch, caplog):
    """A send that raises (transient) or returns success=False must NEVER abort the
    sweep — _owner_notify swallows it with a loud log and returns cleanly."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    approved = tr.TemplateEntry(
        template_name="trial_subscribe_link", language="en",
        content_sid="HX" + "0" * 32, audience="owner",
        variables=("owner_name", "subscribe_link"),
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: approved)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: "+919812345678",
    )

    def _raising_send(*a, **k):
        raise RuntimeError("twilio 5xx")

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(  # must not raise
            tid, "trial_subscribe_link", "en",
            {"owner_name": "x", "subscribe_link": "y"},
            send_fn=_raising_send,
        )
    assert any("owner-notify FAILED" in r.message for r in caplog.records)

    # success=False path (e.g. template_not_yet_approved) — also no crash, loud warn.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_subscribe_link", "en",
            {"owner_name": "x", "subscribe_link": "y"},
            send_fn=lambda *a, **k: _FakeSendResult(
                success=False, error_code="template_not_yet_approved"
            ),
        )
    assert any(
        "owner-notify NOT sent" in r.message and "template_not_yet_approved" in r.message
        for r in caplog.records
    )
