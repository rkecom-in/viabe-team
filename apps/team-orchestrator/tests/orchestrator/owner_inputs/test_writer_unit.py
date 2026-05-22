"""Owner-input extraction writer unit tests (VT-146).

Pure-Python — no DB, no DBOS, no real Anthropic call. Exercises:

- ``classify_message``'s parse contract (valid JSON, fenced JSON,
  invalid intent → ``unclassified``, SDK error → ``unclassified``).
- ``OwnerInputClassification`` shape — only derived fields, no body.
- The ``write_owner_input`` call surface explicitly has no ``body``
  parameter — schema-level proof that the writer cannot accidentally
  persist raw message text.

Runs in the lightweight CI ``test`` job and gates every PR.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

from orchestrator.owner_inputs.writer import (  # noqa: E402
    OwnerInputClassification,
    _ALLOWED_INTENTS,
    _UNCLASSIFIED_SENTINEL,
    classify_message,
    write_owner_input,
)


_SECRET_BODY = "REDACT-PROBE-can-you-promote-our-Diwali-special?"


def _fake_response(text: str) -> Any:
    """Build a SimpleNamespace shaped like an Anthropic Message response."""

    class _TextBlock(SimpleNamespace):
        pass

    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=50, output_tokens=20),
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
    )


def _fake_client(response: Any) -> Any:
    fake = MagicMock()
    fake.messages.create.return_value = response
    return fake


# --- classify_message contract ---------------------------------------------


def test_classify_message_parses_valid_json(monkeypatch):
    """Valid JSON with a known intent → fields land on the dataclass."""
    monkeypatch.setenv("VIABE_ENV", "test")
    response = _fake_response(
        '{"intent": "winback", "segment": "dormant_60d", '
        '"occasion": "diwali"}'
    )
    client = _fake_client(response)

    out = classify_message(_SECRET_BODY, client=client)

    assert out.intent == "winback"
    assert out.segment == "dormant_60d"
    assert out.occasion == "diwali"


def test_classify_message_tolerates_markdown_fence(monkeypatch):
    """Borderline models intermittently wrap JSON in ``` fences even when
    told not to. Tolerate one level of fence; reject loose prose."""
    monkeypatch.setenv("VIABE_ENV", "test")
    fenced = (
        '```json\n{"intent": "feedback", "segment": null, "occasion": null}\n```'
    )
    client = _fake_client(_fake_response(fenced))

    out = classify_message(_SECRET_BODY, client=client)

    assert out.intent == "feedback"
    assert out.segment is None
    assert out.occasion is None


def test_classify_message_returns_unclassified_on_unknown_intent(monkeypatch):
    """An intent value outside ``_ALLOWED_INTENTS`` is coerced to the
    ``unclassified`` sentinel — never silently accepted."""
    monkeypatch.setenv("VIABE_ENV", "test")
    response = _fake_response(
        '{"intent": "promote_my_shop_special", "segment": null, '
        '"occasion": null}'
    )
    client = _fake_client(response)

    out = classify_message(_SECRET_BODY, client=client)

    assert out.intent == _UNCLASSIFIED_SENTINEL
    assert _UNCLASSIFIED_SENTINEL not in _ALLOWED_INTENTS


def test_classify_message_returns_unclassified_on_unparseable_text(monkeypatch):
    """Free-form prose that is not JSON → unclassified. Locks against
    a regression that loosens parsing into ``first { to last }``
    extraction, which would silently invent classifications."""
    monkeypatch.setenv("VIABE_ENV", "test")
    client = _fake_client(_fake_response("I think the answer is winback maybe"))

    out = classify_message(_SECRET_BODY, client=client)

    assert out.intent == _UNCLASSIFIED_SENTINEL


def test_classify_message_returns_unclassified_on_sdk_error(monkeypatch):
    """SDK errors must not bubble — the writer is best-effort. The
    inbound pipeline depends on this contract."""
    monkeypatch.setenv("VIABE_ENV", "test")
    fake = MagicMock()
    fake.messages.create.side_effect = RuntimeError("upstream SDK exploded")

    out = classify_message(_SECRET_BODY, client=fake)

    assert out.intent == _UNCLASSIFIED_SENTINEL


def test_classify_message_short_circuits_on_empty_body(monkeypatch):
    """Empty / whitespace body → ``unclassified``, no SDK call made."""
    monkeypatch.setenv("VIABE_ENV", "test")
    fake = MagicMock()

    out_empty = classify_message("", client=fake)
    out_blank = classify_message("   ", client=fake)

    assert out_empty.intent == _UNCLASSIFIED_SENTINEL
    assert out_blank.intent == _UNCLASSIFIED_SENTINEL
    fake.messages.create.assert_not_called()


# --- Dataclass shape — derived only ----------------------------------------


def test_owner_input_classification_has_no_body_field():
    """OwnerInputClassification shape: intent / segment / occasion only.
    Locking against a future edit that adds a ``body`` / ``raw_text``
    attribute, which would create a path for body to enter the writer's
    call surface and from there into the DB."""
    fields = {f.name for f in OwnerInputClassification.__dataclass_fields__.values()}
    assert fields == {"intent", "segment", "occasion"}
    forbidden = {"body", "raw_text", "content", "message_body", "message_text"}
    assert fields.isdisjoint(forbidden)


def test_write_owner_input_signature_has_no_body_parameter():
    """``write_owner_input``'s call surface accepts only derived fields
    + provenance handles. Adding a ``body`` parameter in a future PR
    must be a deliberate, schema-coupled change — this assertion is the
    canary that forces a review."""
    sig = inspect.signature(write_owner_input)
    param_names = set(sig.parameters.keys())
    forbidden = {"body", "raw_text", "content", "message_body", "message_text"}
    assert param_names.isdisjoint(forbidden), (
        f"write_owner_input gained a forbidden parameter: "
        f"{param_names & forbidden}"
    )
    # Positive assertion — the expected derived + provenance surface.
    assert {"tenant_id", "run_id", "message_sid", "classification"} <= param_names
