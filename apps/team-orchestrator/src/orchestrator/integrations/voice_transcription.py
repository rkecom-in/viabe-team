"""VT-59 — voice-note transcription primitive (Sarvam ASR).

The owner sends a voice note narrating cash-book entries ("Rajesh paid 500,
Sunita paid 300…"). This transcribes it to text; the cash_book method then parses
that text into entries (reusing the VT-63 owner-typed extractor).

Vendor: SARVAM (Cowork VT-59 ruling 2026-06-01) — Indic ASR, India data residency
(DPDP-favorable, CL-422 Mumbai-prod). The model id is env-driven via
config/models.yaml[voice_transcription] (same convention as vision_extraction).

Consent (CL-390 / CL-425): a voice note carries the owner's CUSTOMERS' PII to
Sarvam (a sub-processor). Transmission is FAIL-CLOSED on tenants.owner_inputs —
no consent → ConsentRejectedError BEFORE any Sarvam call. owner_inputs is the
sufficient basis for AI sub-processor transmission (CL-425); Sarvam is added to
the VT-272 privacy-notice sub-processor list. Dev/canary = SYNTHETIC audio only
(CL-422).

The audio bytes are passed IN (the Twilio media-URL fetch is the webhook layer's
job — VT-3.3 — same boundary as the VT-55 image methods, which take bytes too).
The HTTP call is injectable (``http_post``) so tests run without the network /
key; the real call is gated on TEAM_SARVAM_API_KEY (the canary fails-not-skips
when the key is present). PII never logged (CL-390).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml

from orchestrator.integrations.vision_extraction import ConsentRejectedError

logger = logging.getLogger(__name__)

# config/models.yaml — parents: [0]=integrations [1]=orchestrator [2]=src [3]=team-orchestrator.
_MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
_SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
_SARVAM_KEY_ENV = "TEAM_SARVAM_API_KEY"

# Sarvam ASR doesn't return per-token confidence; a non-empty transcript from
# structured ASR is treated as high-confidence. Per-FIELD confidence comes later
# from the owner-typed extractor that parses the transcript.
_TRANSCRIPT_CONF = 0.9

# (audio_bytes, media_type, model, language, api_key) -> Sarvam JSON response.
HttpPost = Callable[[bytes, str, str, str, str], dict[str, Any]]


class SarvamConfigError(Exception):
    """Raised when TEAM_SARVAM_API_KEY is absent (real transcription can't run)."""


class TranscriptionError(Exception):
    """Raised when Sarvam returns empty / non-conforming output (→ caller re-asks)."""


@dataclass(frozen=True)
class TranscriptionResult:
    """A transcript + provenance. No raw audio retained."""

    transcript_text: str
    language: str
    confidence: float


def _resolve_model() -> str:
    """Sarvam model id per VIABE_ENV (config/models.yaml[voice_transcription])."""
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["voice_transcription"][slot])


def _default_post(
    audio_bytes: bytes, media_type: str, model: str, language: str, api_key: str
) -> dict[str, Any]:
    """Real Sarvam STT call (httpx). Multipart upload; Indic-aware."""
    import httpx

    files = {"file": ("audio", audio_bytes, media_type)}
    data = {"model": model, "language_code": language}
    resp = httpx.post(
        _SARVAM_STT_URL, files=files, data=data,
        headers={"api-subscription-key": api_key}, timeout=60.0,
    )
    resp.raise_for_status()
    return cast("dict[str, Any]", resp.json())


def transcribe(
    audio_bytes: bytes,
    *,
    tenant_id: UUID | str,
    media_type: str = "audio/ogg",
    language: str = "unknown",
    consent_check: Callable[[UUID], bool] | None = None,
    http_post: HttpPost | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> TranscriptionResult:
    """Transcribe a voice note via Sarvam (consent-gated, fail-closed).

    Raises ConsentRejectedError (no transmission) if owner_inputs disabled,
    SarvamConfigError if the API key is absent, TranscriptionError on empty/bad
    output. ``language='unknown'`` lets Sarvam auto-detect (en/hi/mixed).
    """
    # CONSENT GATE — fail-closed BEFORE any transmission (CL-390/CL-425).
    if consent_check is None:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        consent_check = _owner_inputs_enabled
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    if not consent_check(tid):
        logger.info("voice_transcription: consent absent (tenant=%s) — not transmitting", tenant_id)
        raise ConsentRejectedError(
            "tenant.owner_inputs disabled — audio NOT transmitted to Sarvam"
        )

    key = api_key or os.environ.get(_SARVAM_KEY_ENV)
    if not key:
        raise SarvamConfigError(
            f"{_SARVAM_KEY_ENV} not set — cannot transcribe (add to .viabe/secrets/team-dev.env)"
        )
    resolved_model = model or _resolve_model()
    post = http_post or _default_post

    payload = post(audio_bytes, media_type, resolved_model, language, key)
    transcript = str(payload.get("transcript") or "").strip()
    if not transcript:
        raise TranscriptionError("Sarvam returned an empty transcript")
    detected = str(payload.get("language_code") or language or "unknown")
    logger.info(
        "voice_transcription: tenant=%s model=%s language=%s chars=%d",
        tenant_id, resolved_model, detected, len(transcript),
    )
    return TranscriptionResult(
        transcript_text=transcript, language=detected, confidence=_TRANSCRIPT_CONF
    )


__all__ = [
    "HttpPost",
    "SarvamConfigError",
    "TranscriptionError",
    "TranscriptionResult",
    "transcribe",
]
