"""VT-59 — Sarvam voice transcription primitive (PURE; no network/key)."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.voice_transcription import (  # noqa: E402
    SarvamConfigError,
    TranscriptionError,
    transcribe,
)


def _post_ok(*_a, **_k):
    return {"transcript": "Rajesh paid 500", "language_code": "en-IN"}


def test_consent_absent_fails_closed_no_transmission():
    from orchestrator.integrations.vision_extraction import ConsentRejectedError

    calls = []

    def _post(*a, **k):
        calls.append(1)
        return {"transcript": "x"}

    with pytest.raises(ConsentRejectedError):
        transcribe(b"audio", tenant_id=uuid4(), consent_check=lambda _t: False,
                   http_post=_post, api_key="k")
    assert not calls  # never transmitted


def test_missing_key_raises_config_error():
    with pytest.raises(SarvamConfigError):
        transcribe(b"audio", tenant_id=uuid4(), consent_check=lambda _t: True,
                   http_post=_post_ok, api_key=None)


def test_empty_transcript_raises():
    with pytest.raises(TranscriptionError):
        transcribe(b"audio", tenant_id=uuid4(), consent_check=lambda _t: True,
                   http_post=lambda *a, **k: {"transcript": "   "}, api_key="k")


def test_happy_path_returns_transcript():
    r = transcribe(b"audio", tenant_id=uuid4(), consent_check=lambda _t: True,
                   http_post=_post_ok, api_key="k")
    assert r.transcript_text == "Rajesh paid 500" and r.language == "en-IN"
    assert 0.0 <= r.confidence <= 1.0
