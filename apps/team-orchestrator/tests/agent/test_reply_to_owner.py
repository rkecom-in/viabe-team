"""VT-632 (Step 1) — reply_to_owner effect-boundary unit tests.

Proves the send boundary the design promised: server-side recipient (no number from the model),
per-turn cap, near-duplicate reject-and-reask (with a short-affirmation exemption), PII redaction
before send (fail = no send), and honest error returns on missing tenant / unresolvable owner /
delivery failure. All external effects are mocked — no DB, no real send.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from orchestrator.agent.tools import reply_to_owner as mod  # noqa: E402

TENANT = uuid4()


def _call(text, state=None):
    """Invoke the tool's underlying function directly (InjectedState is graph-injected at runtime;
    for a unit test we pass the state dict straight in)."""
    st = {"tenant_id": TENANT, "messages": []} if state is None else state
    return mod.reply_to_owner.func(text=text, state=st)


@pytest.fixture(autouse=True)
def _patch_boundary(monkeypatch):
    """Default happy-path mocks: registry absent (⇒ REAL pattern redaction runs, hash_long_body
    False so text is preserved), no dup, owner phone resolves, send succeeds."""
    monkeypatch.setattr("orchestrator.agent.dispatch._registry_for_tenant", lambda t: None)
    monkeypatch.setattr("orchestrator.agent.dispatch._reply_repeats_recent", lambda t, s, **k: False)
    monkeypatch.setattr(mod, "_resolve_owner_phone", lambda t: "+910000000000")
    sends: list[tuple] = []

    def _fake_send(tenant_id, recipient, body):
        sends.append((tenant_id, recipient, body))
        return True

    monkeypatch.setattr("orchestrator.owner_surface.freeform_acks.send_freeform_ack", _fake_send)
    return sends


def test_happy_path_sends_and_returns_sent(_patch_boundary):
    out = _call("Namaste! Aapke 8 customers mein se 2 lapsed ho gaye hain.")
    assert out == "sent"
    assert len(_patch_boundary) == 1
    _tenant, recipient, body = _patch_boundary[0]
    assert recipient == "+910000000000"  # resolved server-side, not from the model
    assert "lapsed" in body


def test_no_tenant_context_errors_without_send(_patch_boundary):
    out = _call("hello", state={"tenant_id": None, "messages": []})
    assert out.startswith("error")
    assert _patch_boundary == []


def test_empty_text_errors_without_send(_patch_boundary):
    assert _call("   ").startswith("error")
    assert _patch_boundary == []


def test_per_turn_cap_blocks_third_send(_patch_boundary):
    prior = [
        SimpleNamespace(name="reply_to_owner", content="sent"),
        SimpleNamespace(name="reply_to_owner", content="sent"),
    ]
    out = _call("a third message this turn", state={"tenant_id": TENANT, "messages": prior})
    assert out.startswith("error")
    assert _patch_boundary == []  # cap hit before any send


def test_refused_attempts_do_not_consume_the_cap(_patch_boundary):
    # An error ToolMessage (content not starting "sent") must not count toward the cap.
    prior = [SimpleNamespace(name="reply_to_owner", content="error: repeated")]
    out = _call("a fresh, first real reply", state={"tenant_id": TENANT, "messages": prior})
    assert out == "sent"


def test_near_duplicate_is_rejected(monkeypatch, _patch_boundary):
    monkeypatch.setattr("orchestrator.agent.dispatch._reply_repeats_recent", lambda t, s, **k: True)
    out = _call("This is a long enough message to be dup-checked properly.")
    assert out.startswith("error")
    assert "repeat" in out.lower()
    assert _patch_boundary == []


def test_short_affirmation_exempt_from_dup_check(monkeypatch, _patch_boundary):
    # Even if the dup-checker would flag it, a short affirmation is exempt and sends.
    monkeypatch.setattr("orchestrator.agent.dispatch._reply_repeats_recent", lambda t, s, **k: True)
    out = _call("Done!")
    assert out == "sent"


def test_redaction_failure_refuses_to_send(monkeypatch, _patch_boundary):
    def _boom(*a, **k):
        raise RuntimeError("redactor down")

    monkeypatch.setattr("orchestrator.privacy.pii_redactor.redact", _boom)
    out = _call("A message that should never go out unredacted.")
    assert out.startswith("error")
    assert _patch_boundary == []  # PII-safe: no send on a redaction failure


def test_long_reply_is_delivered_as_text_not_hash_token(_patch_boundary):
    """VT-632 HOLE-2 regression guard: a benign reply over the 200-char whole-body-hash threshold
    must reach the owner as its TEXT, never a '<body:hash:...>' token."""
    long_body = (
        "Namaste! Aapke business ke liye ek update hai. Aapke 8 customers mein se 2 lapsed ho gaye "
        "hain, aur maine ek win-back plan taiyaar kiya hai jisse unhe wapas laaya ja sake. Aap "
        "chaaho to main aage badhun?"
    )
    assert len(long_body) > 200
    out = _call(long_body)
    assert out == "sent"
    _tenant, _recipient, delivered = _patch_boundary[0]
    assert not delivered.startswith("<body:hash:")
    assert "win-back" in delivered  # the real text, not a token


def test_body_hash_token_is_refused(monkeypatch, _patch_boundary):
    """If redaction ever yields a body-hash token, the tool must REFUSE (never ship the token)."""
    monkeypatch.setattr(
        "orchestrator.privacy.pii_redactor.redact",
        lambda *a, **k: "<body:hash:deadbeef>",
    )
    out = _call("some normal-length owner reply that got hashed somehow")
    assert out.startswith("error")
    assert _patch_boundary == []


def test_unresolvable_owner_phone_errors_without_send(monkeypatch, _patch_boundary):
    monkeypatch.setattr(mod, "_resolve_owner_phone", lambda t: None)
    out = _call("hello there owner, this is a normal reply")
    assert out.startswith("error")
    assert _patch_boundary == []


def test_delivery_failure_reports_error(monkeypatch, _patch_boundary):
    monkeypatch.setattr(
        "orchestrator.owner_surface.freeform_acks.send_freeform_ack",
        lambda tenant_id, recipient, body: False,
    )
    out = _call("a normal reply that fails to deliver (window closed)")
    assert out.startswith("error")


def test_counts_only_successful_prior_sends():
    msgs = [
        SimpleNamespace(name="reply_to_owner", content="sent"),
        SimpleNamespace(name="reply_to_owner", content="error: dup"),
        SimpleNamespace(name="other_tool", content="sent"),
        SimpleNamespace(name="reply_to_owner", content="sent"),
    ]
    assert mod._count_prior_sends(msgs) == 2


def test_reply_tool_already_sent_uses_in_process_fact():
    """VT-632 double-send/silence fix: the scrape-skip is decided from an in-process 'sent'
    ToolMessage in terminal_state (fail-CLOSED), not a fail-soft DB read."""
    from orchestrator.agent.dispatch import _reply_tool_already_sent

    assert _reply_tool_already_sent({"messages": []}) is False
    assert _reply_tool_already_sent({}) is False  # pre-VT-632 / no messages key → scrape fires
    assert (
        _reply_tool_already_sent(
            {"messages": [SimpleNamespace(name="reply_to_owner", content="sent")]}
        )
        is True
    )
    # a REFUSED attempt (dup/cap/error) is not a delivery → scrape must still fire
    assert (
        _reply_tool_already_sent(
            {"messages": [SimpleNamespace(name="reply_to_owner", content="error: dup")]}
        )
        is False
    )
