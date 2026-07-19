"""VT-426 (Row C, hardened) — the trial-sweep owner-notify seam (``trial_sweep._owner_notify``).

These are pure-seam tests: NO DB pool, NO real Twilio. The recipient read
(``get_tenant_whatsapp_number``) is monkeypatched and the send is injected via
``send_fn`` (a recorder), so the whole file is dep-less-safe and runs with 0 external
calls. The point under test is the WIRING + the VT-426 FAIL-CLOSED gates:

  - GATE 1 (approved_for_live, PRIMARY): when a template entry has approved_for_live=False
    → _owner_notify SKIPS, 0 sends, even though the SID is real and Meta-approved. This
    is the deploy-safe gate proof. VT-430: the two trial templates now have
    approved_for_live=True (flipped by DEV-POSTURE-REVERSE §3); this gate test uses a
    stub entry (not the real registry) to keep the gate coverage valid.
  - GATE 2 (audience): approved_for_live=true but audience != owner → SKIP.
  - GATE 3 (validate_params): approved_for_live=true + owner audience but a
    missing/empty required positional → SKIP (the VT-400 "Hi Raj Cafe" sample bug).
  - all gates pass (approved + owner + complete params) → SENDS ONCE (recorded send_fn),
    owner_name populated.
  - VT-430 enablement proofs: trial_ending with owner_name resolved + trial_subscribe_link
    with owner_name + subscribe_link → SENDS with correct populated params; a tenant with
    NO resolvable owner_name → GATE 3 still fail-closes (no send).
  - retained fail-safes: UNREGISTERED template, pending-approval stub SID, no recipient,
    a raising / success=False send — all SKIP/swallow loudly, the sweep never aborts.
"""

from __future__ import annotations

import logging
import uuid

import pytest

# The code under test (trial_sweep → attribution_close) imports psycopg; the dep-less smoke
# (CI 'test' job + pre-push) runs without heavy deps, so skip there. Runs fully with deps. (VT-447 sweep)
pytest.importorskip("psycopg")


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
    """THE KEY DEPLOY-SAFE GATE TEST. When a template entry has approved_for_live=False
    → _owner_notify SKIPS with a loud log and makes 0 sends. The recipient is NOT even
    read — GATE 1 short-circuits first. Uses a monkeypatched entry (not the real
    registry) because VT-430 flipped the two trial templates to approved_for_live=True;
    the gate logic itself is unchanged and this test keeps it covered."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    def _should_not_send(*a, **k):
        sends.append((a, k))
        return _FakeSendResult(success=True)

    # Stub entry with approved_for_live=False — exercises the gate independent of yaml.
    not_approved = _entry(
        template_name="trial_subscribe_link",
        variables=("owner_name", "subscribe_link"),
        approved_for_live=False,
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: not_approved)

    # Recipient must NOT even be read — GATE 1 short-circuits first.
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before approved gate")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid,
            "trial_subscribe_link",
            "en",
            {"owner_name": "Raj Cafe", "subscribe_link": "https://viabe.ai/team/subscribe?t=tok"},
            send_fn=_should_not_send,
        )

    assert sends == []  # 0 sends — gate working
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


# ---------------------------------------------------------------------------
# VT-430 — enablement proofs (approved_for_live now TRUE for both trial templates)
# ---------------------------------------------------------------------------


def test_vt430_trial_ending_sends_with_populated_owner_name(monkeypatch):
    """VT-430 ENABLEMENT PROOF — trial_ending. approved_for_live=true (as of VT-430) +
    audience=owner + complete params (owner_name populated from the tenant row, NOT a
    sample value) + valid recipient → SENDS ONCE. owner_name is the real business name,
    not the Twilio sample ("Raj Cafe" here, resolved via _resolve_owner_name in production).

    This is the exact correctness proof required by VT-430: with the yaml flip the daily
    cron's warn path fires a real owner WhatsApp message with {{1}} rendered from the
    tenant's business_name, not a sample placeholder."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    calls: list[tuple] = []

    def _recording_send(t, template_name, language, params, *, recipient_phone):
        calls.append((str(t), template_name, language, params, recipient_phone))
        return _FakeSendResult(success=True)

    # Real trial_ending entry shape — approved + owner + complete params.
    trial_ending_entry = _entry(
        template_name="trial_ending",
        variables=("owner_name", "trial_end_date"),
        approved_for_live=True,  # VT-430 flipped this
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: trial_ending_entry)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: "+919812345678",
    )

    # Params as built by run_trial_evaluation_body: owner_name from _resolve_owner_name.
    ts._owner_notify(
        tid,
        "trial_ending",
        "en",
        {"owner_name": "Raj Cafe", "trial_end_date": "2026-07-14"},
        send_fn=_recording_send,
    )

    assert len(calls) == 1, "expected exactly ONE send"
    _tid, tpl, lang, params, recipient = calls[0]
    assert tpl == "trial_ending"
    assert params["owner_name"] == "Raj Cafe"  # {{1}} = real name, NOT a Twilio sample
    assert params["trial_end_date"] == "2026-07-14"  # {{2}} populated
    assert recipient == "+919812345678"


def test_vt430_trial_subscribe_link_sends_with_populated_params(monkeypatch):
    """VT-430 ENABLEMENT PROOF — trial_subscribe_link. approved_for_live=true (as of
    VT-430) + audience=owner + complete params (owner_name + subscribe_link both
    non-empty) + valid recipient → SENDS ONCE with correct param values."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    calls: list[tuple] = []

    def _recording_send(t, template_name, language, params, *, recipient_phone):
        calls.append((str(t), template_name, language, params, recipient_phone))
        return _FakeSendResult(success=True)

    # Real trial_subscribe_link entry shape — approved + owner.
    trial_sub_entry = _entry(
        template_name="trial_subscribe_link",
        variables=("owner_name", "subscribe_link"),
        approved_for_live=True,  # VT-430 flipped this
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: trial_sub_entry)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: "+919812345678",
    )

    link = "https://viabe.ai/team/subscribe?token=abc123"
    ts._owner_notify(
        tid,
        "trial_subscribe_link",
        "hi",
        {"owner_name": "Raj Cafe", "subscribe_link": link},
        send_fn=_recording_send,
    )

    assert len(calls) == 1, "expected exactly ONE send"
    _tid, tpl, lang, params, recipient = calls[0]
    assert tpl == "trial_subscribe_link"
    assert lang == "hi"  # preferred language variant respected
    assert params["owner_name"] == "Raj Cafe"  # populated, not a sample
    assert params["subscribe_link"] == link
    assert recipient == "+919812345678"


def test_vt430_no_owner_name_still_fail_closes(monkeypatch, caplog):
    """VT-430 CORRECTNESS KEPT — even with approved_for_live=true, a tenant with NO
    resolvable owner_name (None from _resolve_owner_name → owner_name=None in params)
    → GATE 3 fail-closes: 0 sends, loud log. The Twilio SAMPLE is never rendered.

    This is the second required test from VT-430: the enablement does NOT degrade safety
    — a tenant without a business_name in the tenants row still gets no send."""
    from orchestrator import templates_registry as tr
    from orchestrator.billing import trial_sweep as ts

    tid = uuid.uuid4()
    sends: list[tuple] = []

    trial_ending_entry = _entry(
        template_name="trial_ending",
        variables=("owner_name", "trial_end_date"),
        approved_for_live=True,  # flag is live, but the name is None
    )
    monkeypatch.setattr(tr, "resolve", lambda name, lang, **k: trial_ending_entry)
    monkeypatch.setattr(
        "orchestrator.utils.twilio_send.get_tenant_whatsapp_number",
        lambda t: (_ for _ in ()).throw(AssertionError("recipient read before param gate")),
    )

    with caplog.at_level(logging.WARNING):
        ts._owner_notify(
            tid,
            "trial_ending",
            "en",
            # owner_name=None — as produced when _resolve_owner_name returns None
            {"owner_name": None, "trial_end_date": "2026-07-14"},
            send_fn=lambda *a, **k: sends.append((a, k)),
        )

    assert sends == [], "GATE 3 must skip — owner_name=None would render the Twilio sample"
    assert any(
        "missing/empty required params" in r.message for r in caplog.records
    ), "expected GATE 3 loud log for None owner_name"
