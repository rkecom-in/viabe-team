"""VT-426 (Row C, hardened) — the trial-sweep owner-notify seam (``trial_sweep._owner_notify``).

These are pure-seam tests: NO DB pool, NO real Twilio. The recipient read
(``get_tenant_whatsapp_number``) is monkeypatched and the send is injected via
``send_fn`` (a recorder), so the whole file is dep-less-safe and runs with 0 external
calls. The point under test is the WIRING + the VT-426 FAIL-CLOSED gates:

  - GATE 1 (approved_for_live, PRIMARY): the trial templates ship with
    ``approved_for_live: false`` → _owner_notify SKIPS, 0 sends, even though the SID is
    real and Meta-approved. THIS is the deploy-safe proof — the daily cron sends NOTHING
    until Fazal flips the flag.
  - GATE 2 (audience): approved_for_live=true but audience != owner → SKIP.
  - GATE 3 (validate_params): approved_for_live=true + owner audience but a
    missing/empty required positional → SKIP (the VT-400 "Hi Raj Cafe" sample bug).
  - all gates pass (approved + owner + complete params) → SENDS ONCE (recorded send_fn),
    owner_name populated.
  - retained fail-safes: UNREGISTERED template, pending-approval stub SID, no recipient,
    a raising / success=False send — all SKIP/swallow loudly, the sweep never aborts.
"""

from __future__ import annotations

import logging
import uuid


class _FakeSendResult:
    """Stand-in for utils.twilio_send.SendResult — only the fields _owner_notify reads."""

    def __init__(self, *, success: bool, error_code: str | None = None) -> None:
        self.success = success
        self.error_code = error_code


def _entry(**overrides):
    """Build a TemplateEntry with the VT-426-passing defaults; override per test."""
    from orchestrator import templates_registry as tr

    base = dict(
        template_name="trial_subscribe_link",
        language="en",
        content_sid="HX" + "0" * 32,
        audience="owner",
        variables=("owner_name", "subscribe_link"),
        approved_for_live=True,
    )
    base.update(overrides)
    return tr.TemplateEntry(**base)


# ---------------------------------------------------------------------------
# GATE 1 — approved_for_live (the deploy-safe PRIMARY gate)
# ---------------------------------------------------------------------------


def test_owner_notify_skips_when_not_approved_for_live(monkeypatch, caplog):
    """THE KEY DEPLOY-SAFE TEST. The shipped trial templates have a real, Meta-approved
    SID but ``approved_for_live: false`` → _owner_notify SKIPS with a loud log and makes
    0 sends. This is the proof that a deploy + the 7 AM cron fires NOTHING until Fazal
    flips the flag. Uses the REAL registry entry (trial_subscribe_link), not a stub."""
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    def _should_not_send(*a, **k):
        sends.append((a, k))
        return _FakeSendResult(success=True)

    # Recipient must NOT even be read — GATE 1 short-circuits first.
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before approved gate")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid,
            "trial_subscribe_link",  # real SID, but approved_for_live: false as shipped
            "en",
            {"owner_name": "Raj Cafe", "subscribe_link": "https://viabe.ai/team/subscribe?t=tok"},
            send_fn=_should_not_send,
        )

    assert sends == []  # 0 sends — deploy-safe
    assert any(
        "SKIP owner-notify" in r.message and "NOT approved_for_live" in r.message
        for r in caplog.records
    ), "expected a loud SKIP/NOT-approved_for_live warning"


def test_owner_notify_sends_when_all_gates_pass(monkeypatch):
    """ALL gates pass — approved_for_live=true + owner audience + complete non-empty
    params → notify resolves the recipient and invokes the send_fn ONCE with the
    preferred-language variant + the params (owner_name populated). 0 real Twilio."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    calls: list[tuple] = []

    def _recording_send(t, template_name, language, params, *, recipient_phone):
        calls.append((str(t), template_name, language, params, recipient_phone))
        return _FakeSendResult(success=True)

    # approved + owner + real SID — the live-approved entry (Fazal flipped the flag).
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: _entry())
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: "+919812345678",
    )

    link = "https://viabe.ai/team/subscribe?t=tok"
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
    assert params["owner_name"] == "Raj Cafe"  # {{1}} populated — no Twilio sample render
    assert params["subscribe_link"] == link
    assert recipient == "+919812345678"


# ---------------------------------------------------------------------------
# GATE 2 — audience enforcement (VULN4)
# ---------------------------------------------------------------------------


def test_owner_notify_skips_non_owner_audience(monkeypatch, caplog):
    """approved_for_live=true but audience != 'owner' (a customer-audience template) →
    SKIP, 0 sends. The owner-notify seam never lands a customer template on an owner."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    monkeypatch.setattr(
        tr, "resolve",
        lambda name, lang, **k: _entry(audience="customer", variables=("owner_name", "trial_end_date")),
    )
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before audience gate")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_ending", "en",
            {"owner_name": "Raj Cafe", "trial_end_date": "2026-07-14"},
            send_fn=lambda *a, **k: sends.append((a, k)),
        )

    assert sends == []
    assert any(
        "SKIP owner-notify" in r.message and "not 'owner'" in r.message
        for r in caplog.records
    ), "expected a loud SKIP/audience warning"


# ---------------------------------------------------------------------------
# GATE 3 — validate_params fail-closed (VULN1)
# ---------------------------------------------------------------------------


def test_owner_notify_skips_missing_required_param(monkeypatch, caplog):
    """approved_for_live=true + owner audience but a declared positional is MISSING →
    SKIP, 0 sends. Never send a template that would render the Twilio SAMPLE (VT-400)."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    monkeypatch.setattr(
        tr, "resolve",
        lambda name, lang, **k: _entry(variables=("owner_name", "subscribe_link")),
    )
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before param gate")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_subscribe_link", "en",
            {"subscribe_link": "https://viabe.ai/team/subscribe?t=tok"},  # owner_name MISSING
            send_fn=lambda *a, **k: sends.append((a, k)),
        )

    assert sends == []
    assert any(
        "SKIP owner-notify" in r.message and "missing/empty required params" in r.message
        for r in caplog.records
    ), "expected a loud SKIP/missing-param warning"


def test_owner_notify_skips_empty_required_param(monkeypatch, caplog):
    """A declared positional present but EMPTY ('') → SKIP (an empty {{1}} also renders
    the Twilio sample). Stricter than key-set equality."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    monkeypatch.setattr(
        tr, "resolve",
        lambda name, lang, **k: _entry(variables=("owner_name", "subscribe_link")),
    )
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before param gate")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid, "trial_subscribe_link", "en",
            {"owner_name": "", "subscribe_link": "https://viabe.ai/team/subscribe?t=tok"},
            send_fn=lambda *a, **k: sends.append((a, k)),
        )

    assert sends == []
    assert any(
        "missing/empty required params" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Retained fail-safes
# ---------------------------------------------------------------------------


def test_owner_notify_skips_unregistered_template_loudly(monkeypatch, caplog):
    """An UNREGISTERED template name → _owner_notify FAIL-SAFE SKIPS with a loud log,
    makes 0 sends, and never even resolves a recipient or crashes."""
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
    """A registered, approved-for-live, owner-audience template whose SID is a
    pending-approval stub (content_sid=None) → FAIL-SAFE SKIP, 0 sends."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    # approved + owner so we reach the SID-None gate (not short-circuited by GATE 1/2).
    pending = _entry(
        template_name="trial_ending",
        content_sid=None,  # pending Meta approval
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
    """All gates pass but the tenant has no reachable whatsapp_number → skip with a loud
    log, 0 sends, no crash."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: _entry())
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
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: _entry())
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
