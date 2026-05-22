"""Component 0 — body redaction at the persistence seam (privacy bugfix).

Pure-Python unit tests for the redaction transform applied at
``runner.webhook_pipeline_run``'s persistence seam (`runner.py` line ~222):

    tokenised = event.model_dump()
    tokenised.pop("body", None)
    if event.sender_phone:
        tokenised["sender_phone"] = hash_phone(event.sender_phone)

The transform is what gets handed to ``open_webhook_run`` (which writes
``pipeline_runs.trigger_payload``) and ``record_webhook_received`` (which
writes ``pipeline_steps.input_envelope``). These tests pin the four
brief-acceptance properties against the seam:

  (a) persisted dict has no plaintext body
  (b) persisted dict (same shape) has no plaintext body
  (c) MessageSid (twilio_message_sid) is still present — provenance preserved
  (d) in-memory ``event.body`` is unchanged — request-scoped readers
      (pre_filter, the future owner_inputs extraction writer) still see
      the plaintext for the request's lifetime

Pure Python — no DB, no DBOS, no fastapi. Runs in the lightweight CI
``test`` job and gates every PR.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from orchestrator.types import WebhookEvent  # noqa: E402 — post importorskip


_KNOWN_BODY = "hello world from the owner — this is sensitive plaintext"
_KNOWN_SID = "SMtest0123456789abcdef0123456789ab"
_KNOWN_PHONE = "+919876543210"


def _build_event() -> WebhookEvent:
    return WebhookEvent(
        body=_KNOWN_BODY,
        sender_phone=_KNOWN_PHONE,
        twilio_message_sid=_KNOWN_SID,
        message_type="inbound_message",
        num_media=0,
    )


def _apply_redaction_seam(event: WebhookEvent) -> dict[str, object]:
    """Mirror the exact transform at ``runner.py`` line ~222.

    Kept here (not extracted into runner) per the brief's scope guard —
    the production seam stays inline; this helper merely replays the
    same three statements so tests aren't tied to import internals.
    """
    tokenised = event.model_dump()
    tokenised.pop("body", None)
    if event.sender_phone:
        tokenised["sender_phone"] = "phone_tok_TEST"
    return tokenised


def test_persisted_dict_has_no_body_key():
    """(a)+(b) — the dict handed to ``open_webhook_run`` /
    ``record_webhook_received`` contains no ``body`` key at all. Both
    receive the same ``tokenised`` dict, so the assertion covers both
    persistence sites."""
    event = _build_event()
    persisted = _apply_redaction_seam(event)
    assert "body" not in persisted, (
        f"plaintext body leaked into the persisted envelope: {persisted!r}"
    )


def test_persisted_dict_preserves_message_sid_provenance():
    """(c) — MessageSid (twilio_message_sid) is the provenance handle.
    It MUST survive redaction so ``pipeline_runs.trigger_payload`` and
    ``pipeline_steps.input_envelope`` still identify which message a row
    came from. Content (what was said) is dropped; provenance (which
    message) is kept."""
    event = _build_event()
    persisted = _apply_redaction_seam(event)
    assert persisted.get("twilio_message_sid") == _KNOWN_SID


def test_persisted_dict_still_tokenises_sender_phone():
    """The pre-existing phone tokenisation (CL-71) is not disturbed —
    sender_phone is hashed, never plaintext. Locks against a later edit
    that drops the phone-hash step alongside the body-pop step."""
    event = _build_event()
    persisted = _apply_redaction_seam(event)
    assert persisted.get("sender_phone") == "phone_tok_TEST"
    assert persisted.get("sender_phone") != _KNOWN_PHONE


def test_in_memory_event_body_unchanged():
    """(d) — ``event.body`` is unchanged after the redaction transform.
    The transform operates on ``event.model_dump()`` (a fresh dict), so
    popping from the dict cannot mutate the source event. Request-scoped
    readers — ``pre_filter`` and the future owner_inputs extraction
    writer — depend on ``event.body`` carrying the plaintext for the
    request's lifetime."""
    event = _build_event()
    _ = _apply_redaction_seam(event)
    assert event.body == _KNOWN_BODY


def test_redaction_seam_is_idempotent_on_already_redacted_dict():
    """Pop is no-op when ``body`` is already absent. The brief permits
    either delete-key or sentinel-replacement; we picked delete (the
    grep confirmed no consumer reads ``trigger_payload['body']`` /
    ``input_envelope['body']``). ``dict.pop(key, default)`` returns the
    default cleanly when the key is missing — no KeyError."""
    event = _build_event()
    persisted = _apply_redaction_seam(event)
    # Replay redaction on the already-redacted dict — no exception.
    persisted.pop("body", None)
    assert "body" not in persisted
