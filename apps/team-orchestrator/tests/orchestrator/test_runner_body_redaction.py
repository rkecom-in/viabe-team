"""Body-key redaction at the persistence boundary (VT-Privacy-Writer-Side).

VT-144 (PR #45) introduced caller-side ``tokenised.pop("body", None)``
in ``webhook_pipeline_run``. This PR moves that redaction to the
persistence boundary — ``runner._redact_for_persistence`` — so any
future caller of ``open_webhook_run`` / ``record_webhook_received``
cannot leak message content into ``pipeline_runs.trigger_payload`` /
``pipeline_steps.input_envelope``.

These tests pin the contract of ``_redact_for_persistence`` directly:

  (a) Body / body-aliases are stripped from the returned dict.
  (b) MessageSid (twilio_message_sid) survives — provenance preserved.
  (c) Non-content metadata (timestamps, channel IDs, hashed phone)
      survives.
  (d) The input dict is NOT mutated — callers can keep using the
      original for request-scoped readers (pre_filter, the gated
      owner_inputs writer).

Pure Python — no DB, no DBOS. Runs in the lightweight CI ``test`` job.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.runner import (  # noqa: E402 — post importorskip
    _REDACTED_KEYS_AT_REST,
    _redact_for_persistence,
)


_KNOWN_BODY = "hello world from the owner — this is sensitive plaintext"
_KNOWN_SID = "SMtest0123456789abcdef0123456789ab"
_KNOWN_PHONE_TOK = "phone_tok_TEST"


def _representative_envelope() -> dict[str, object]:
    """A WebhookEvent.model_dump()-shape dict — what
    ``webhook_pipeline_run`` hands to the writers."""
    return {
        "body": _KNOWN_BODY,
        "sender_phone": _KNOWN_PHONE_TOK,
        "twilio_message_sid": _KNOWN_SID,
        "message_type": "inbound_message",
        "num_media": 0,
        "media_url_0": None,
    }


def test_redact_strips_body_key():
    """(a) — ``body`` is removed from the returned envelope. Brief's
    primary assertion: trigger_payload and input_envelope contain no
    body field."""
    envelope = _representative_envelope()
    safe = _redact_for_persistence(envelope)
    assert "body" not in safe, (
        f"plaintext body leaked into persisted envelope: {safe!r}"
    )


def test_redact_strips_body_aliases():
    """Body-key alias coverage — defensive against a future code path
    that names message content under a synonym. Locks against silent
    rename regression."""
    envelope = _representative_envelope() | {
        "message_body": "alias 1",
        "raw_text": "alias 2",
        "content": "alias 3",
    }
    safe = _redact_for_persistence(envelope)
    for alias in ("body", "message_body", "raw_text", "content"):
        assert alias not in safe, (
            f"body alias {alias!r} leaked into persisted envelope"
        )


def test_redact_preserves_message_sid_provenance():
    """(b) — MessageSid (``twilio_message_sid``) is the provenance handle
    that ties a row back to the WhatsApp message. Brief's second
    assertion: MessageSid IS still present (guard against
    over-redaction breaking provenance)."""
    envelope = _representative_envelope()
    safe = _redact_for_persistence(envelope)
    assert safe.get("twilio_message_sid") == _KNOWN_SID


def test_redact_preserves_non_content_metadata():
    """(c) — All non-content keys survive: hashed phone, message_type,
    media counts. Over-redaction would break downstream observability
    + idempotency lookups; lock the survivors."""
    envelope = _representative_envelope()
    safe = _redact_for_persistence(envelope)
    assert safe.get("sender_phone") == _KNOWN_PHONE_TOK
    assert safe.get("message_type") == "inbound_message"
    assert safe.get("num_media") == 0
    assert "media_url_0" in safe


def test_redact_does_not_mutate_input():
    """(d) — input dict is NOT mutated. The caller keeps the full
    envelope in memory for request-scoped readers (pre_filter consumes
    body; owner_inputs writer consumes body when its SHIP GATE clears).
    ``_redact_for_persistence`` returns a fresh dict; the original
    must still carry body."""
    envelope = _representative_envelope()
    _ = _redact_for_persistence(envelope)
    assert envelope.get("body") == _KNOWN_BODY


def test_redact_is_idempotent_on_already_redacted_dict():
    """A second call against an already-redacted dict is a no-op (and
    no KeyError). Reflects how the function would behave if a future
    refactor doubled the call site."""
    envelope = _representative_envelope()
    once = _redact_for_persistence(envelope)
    twice = _redact_for_persistence(once)
    assert "body" not in twice
    assert twice.get("twilio_message_sid") == _KNOWN_SID


def test_redacted_keys_set_includes_body():
    """Sanity: the module-level forbidden-keys set actually contains
    ``body``. Locks against a future edit that empties the set or
    drops the canonical key."""
    assert "body" in _REDACTED_KEYS_AT_REST
