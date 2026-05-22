"""VT-146 real-API canary for the owner-input classifier.

Single-mode (Haiku) because production AND test both resolve to Haiku
in ``models.yaml`` — there is no Opus path for structured-extraction
classification. The two-mode pattern from
DECISION 366387c2-cc5a-81e9-9f79-f356f5618502 collapses to one mode here.

Proof-of-call discipline (CL-272): the canary MUST prove a real billed
Anthropic call happened — a green pass that could be reached by a mock
leak or skipped-but-passed shape is a dead canary and is rejected. The
assertions below are load-bearing:

  (1) ``messages.create`` reached the real ``anthropic.Anthropic`` class
      (NOT a MagicMock leftover from another fixture).
  (2) The response carries a real ``msg_*`` id — only real API responses
      do; the local fake-response namespace doesn't synthesise this.
  (3) Real usage tokens are reported.
  (4) Wall-clock floor > 0.5s — distinguishes a real network round-trip
      from a near-instant mock.

Env-gated on ``VIABE_RUN_OWNER_INPUT_CANARY=1`` + ``ANTHROPIC_API_KEY``.
CI never burns API quota — the canary runs pre-merge with secrets, or
manually before sign-off.
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")


def _canary_skip_reason() -> str:
    return (
        "owner_input classifier canary skipped — set "
        "VIABE_RUN_OWNER_INPUT_CANARY=1 + ANTHROPIC_API_KEY"
    )


@pytest.mark.skipif(
    os.environ.get("VIABE_RUN_OWNER_INPUT_CANARY") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY"),
    reason=_canary_skip_reason(),
)
def test_canary_real_haiku_classification(monkeypatch):
    """Real Haiku call. Asserts proof-of-call invariants + that the
    classifier returns a valid intent (any value in the allowed set is
    a PASS — the goal is plumbing + a well-formed verdict, not pinning
    Haiku's judgment on a specific message).

    The probe message names a 'Diwali winback campaign' — straightforward
    enough that Haiku reliably picks ``winback`` or ``campaign_request``.
    Both are PASS; we assert on the well-formed-verdict contract, not
    the specific value.
    """
    from anthropic import Anthropic as _RealAnthropic

    from orchestrator.owner_inputs.writer import (
        _ALLOWED_INTENTS,
        OwnerInputClassification,
        classify_message,
    )

    # Sanity — the real SDK class is in scope, not a MagicMock leak.
    assert _RealAnthropic.__module__.startswith("anthropic"), (
        f"anthropic.Anthropic non-genuine: module="
        f"{_RealAnthropic.__module__!r}"
    )

    class _CountingClient:
        """Real SDK + ledger — records every ``messages.create`` call
        AFTER it returns. The ledger is what proves a real billed call
        happened: ``response_id`` (``msg_*``), ``input_tokens``,
        ``output_tokens``, and the model the call hit."""

        calls: list[dict[str, Any]] = []

        def __init__(self) -> None:
            self._real = _RealAnthropic()

        @property
        def messages(self):  # type: ignore[no-untyped-def]
            return self

        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            response = self._real.messages.create(**kwargs)
            _CountingClient.calls.append(
                {
                    "model": kwargs.get("model"),
                    "response_id": getattr(response, "id", None),
                    "response_usage_input": getattr(
                        getattr(response, "usage", None), "input_tokens", None
                    ),
                    "response_usage_output": getattr(
                        getattr(response, "usage", None), "output_tokens", None
                    ),
                }
            )
            return response

    _CountingClient.calls = []
    # VIABE_ENV=test → Haiku per models.yaml (both slots are Haiku
    # anyway, but stay deterministic).
    monkeypatch.setenv("VIABE_ENV", "test")
    client = _CountingClient()

    probe_body = (
        "We have a lot of dormant customers from before Diwali — can you "
        "send them a winback campaign with a 15% off coupon?"
    )

    wallclock_start = time.monotonic()
    out = classify_message(probe_body, client=client)
    wallclock_s = time.monotonic() - wallclock_start

    diag = {
        "wallclock_s": wallclock_s,
        "intent": out.intent,
        "segment": out.segment,
        "occasion": out.occasion,
        "call_count": len(_CountingClient.calls),
        "call_ledger": _CountingClient.calls,
    }

    # --- Proof of call -------------------------------------------------
    # (1) Real call reached the SDK.
    assert len(_CountingClient.calls) == 1, diag
    first = _CountingClient.calls[0]
    # (2) Haiku was the target — production slot pin in models.yaml.
    assert first["model"] == "claude-haiku-4-5", diag
    # (3) ``msg_*`` id prefix proves real Anthropic API response.
    assert isinstance(first["response_id"], str), diag
    assert first["response_id"].startswith("msg_"), diag
    # (4) Real usage tokens — the local fake-response namespace would
    # have hard-coded these; a real Haiku call yields varying counts.
    assert isinstance(first["response_usage_input"], int), diag
    assert first["response_usage_input"] > 0, diag
    assert isinstance(first["response_usage_output"], int), diag
    assert first["response_usage_output"] > 0, diag
    # (5) Wall-clock floor — real network round-trip vs near-instant mock.
    assert wallclock_s > 0.5, diag

    # --- Verdict shape -------------------------------------------------
    assert isinstance(out, OwnerInputClassification), diag
    # The classifier produced a well-formed verdict. ``unclassified``
    # would mean the parse failed — that is a real signal worth seeing,
    # so capture it in the diag and fail the canary if it fires (Haiku
    # should not fail on this clearly-classifiable probe).
    assert out.intent in _ALLOWED_INTENTS, diag
    # Surface the verdict + diag for the CI run output regardless of
    # which intent Haiku picked.
    print(
        "VT146_CANARY_VERDICT:",
        out.intent,
        "segment:",
        out.segment,
        "occasion:",
        out.occasion,
        "wallclock_s:",
        round(wallclock_s, 2),
        "msg_id:",
        first["response_id"],
    )
    # Silence "unused" — uuid4 is imported for parallel structure with
    # other canaries.
    _ = uuid4
