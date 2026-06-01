"""VT-59 / VT-6 Method 5 — cash-book photograph + voice note (multimodal).

The owner sends a photo of their handwritten cash book and/or a voice note
narrating the entries. Three paths over the shared primitives (Pillar 8):
  - image-only  → ingest_image (vision → entries), degrades to the paper-book path.
  - audio-only  → Sarvam transcription → the VT-63 owner-typed extractor parses the
                  narration into entries (reuse — narration is just typed text spoken).
  - both        → vision extraction + transcription → a Sonnet multimodal MERGE that
                  reconciles the two (confirmed-by-both → 0.95, single-source → the
                  lower confidence, conflict → 0.5 → clarify).

All paths then route through the shared ingest_entries with
park_unattributed=True (VT-58 seam): attributed entries → dedup + ledger;
unattributed-with-amount → parked in imported_transactions for VT-275.
acquired_via='cash_book'.

Consent (CL-390/CL-425): the transcription primitive + the owner-typed extractor +
vision each fail-closed on owner_inputs before their own sub-processor send. Audio
bytes are passed IN (the Twilio media-URL fetch is the webhook layer's job, VT-3.3
— the same boundary as the VT-55 image methods, which take bytes). Sarvam +
Anthropic are the sub-processors (VT-272 privacy-notice list). PII never logged.
CL-422 dev = synthetic only.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml
from anthropic import Anthropic

from orchestrator.integrations.methods._image_adapter import (
    TARGET_FIELDS,
    IngestionSummary,
    ingest_entries,
    ingest_image,
)
from orchestrator.integrations.methods.contacts import _normalize_phone
from orchestrator.integrations.methods.owner_typed import extract_owner_typed
from orchestrator.integrations.vision_extraction import (
    ExtractedField,
    ExtractionResult,
    extract_entries_from_image,
)
from orchestrator.integrations.voice_transcription import TranscriptionResult, transcribe

logger = logging.getLogger(__name__)

_MODELS_YAML = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
_MERGE_PROMPT = (
    Path(__file__).resolve().parents[2] / "agent" / "prompts" / "cash_book_merge_v1.md"
)
_MAX_OUTPUT_TOKENS = 2048

TranscribeFn = Callable[..., TranscriptionResult]


def _resolve_merge_model() -> str:
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["cash_book_merge"][slot])


def _fields_from_rows(rows: list[dict[str, Any]]) -> tuple[ExtractedField, ...]:
    """JSON merge rows → ExtractedFields, normalising phone to E.164 (cross-method dedup)."""
    fields: list[ExtractedField] = []
    for r in rows:
        name = str(r["name"])
        raw = r.get("value")
        value = None if raw in (None, "") else str(raw)
        conf = float(r["confidence"])
        if name == "phone" and value is not None:
            e164, norm_conf = _normalize_phone(value)
            value = e164
            conf = min(conf, norm_conf) if e164 is not None else 0.0
        fields.append(ExtractedField(name=name, value=value, confidence=conf))
    return tuple(fields)


def _parse_merge_response(raw: str, model: str) -> list[ExtractionResult]:
    """Parse the merge model's JSON into ExtractionResults (P8: no regex-scrub)."""
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if not raw:
        return []
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return []  # unreconcilable output → nothing committed (caller re-asks)
    entries = parsed.get("entries") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return []
    results: list[ExtractionResult] = []
    for ent in entries:
        rows = ent.get("fields") if isinstance(ent, dict) else None
        if not isinstance(rows, list):
            continue
        try:
            results.append(ExtractionResult(
                fields=_fields_from_rows(rows), acquired_via="cash_book", model=model
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return results


def _merge_photo_and_voice(
    vision_results: list[ExtractionResult],
    transcript: str,
    *,
    client: Anthropic | None,
    model: str | None,
) -> list[ExtractionResult]:
    """Sonnet reconcile of photo entries vs narration → merged ExtractionResults."""
    base = _MERGE_PROMPT.read_text(encoding="utf-8")
    photo_json = json.dumps(
        {"entries": [
            {"fields": [
                {"name": f.name, "value": f.value, "confidence": f.confidence}
                for f in r.fields
            ]} for r in vision_results
        ]},
        ensure_ascii=False,
    )
    prompt = (
        f"{base}\n\nPHOTO EXTRACTION (JSON):\n{photo_json}\n\n"
        f"OWNER NARRATION (transcript):\n{transcript}\n"
    )
    if client is None:
        client = Anthropic()
    resolved = model or _resolve_merge_model()
    resp = client.messages.create(
        model=resolved, max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )
    text = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    return _parse_merge_response(text, resolved)


def ingest_cash_book(
    tenant_id: UUID | str,
    *,
    image_bytes: bytes | None = None,
    audio_bytes: bytes | None = None,
    image_media_type: str = "image/jpeg",
    audio_media_type: str = "audio/ogg",
    now: datetime | None = None,
    run_id: str | None = None,
    consent_check: Callable[[UUID], bool] | None = None,
    anthropic_client: Anthropic | None = None,
    transcribe_fn: TranscribeFn | None = None,
    image_extract_fn: Callable[..., list[ExtractionResult]] = extract_entries_from_image,
    merge_model: str | None = None,
) -> IngestionSummary:
    """Ingest a cash-book photo and/or voice note. tenant_id from context (P3).

    At least one of image_bytes / audio_bytes is required. Returns counts only.
    Injectables (consent_check / anthropic_client / transcribe_fn / image_extract_fn
    / merge_model) keep it testable without network or keys.
    """
    if not image_bytes and not audio_bytes:
        raise ValueError("ingest_cash_book: need image_bytes and/or audio_bytes")
    now = now or datetime.now(UTC)
    transcribe_fn = transcribe_fn or transcribe

    # Image-only → the paper-book vision path (parks unattributed).
    if image_bytes and not audio_bytes:
        return ingest_image(
            tenant_id, image_bytes, acquired_via="cash_book",
            media_type=image_media_type, now=now, park_unattributed=True,
            extract_fn=image_extract_fn,
            consent_check=consent_check, client=anthropic_client,
        )

    assert audio_bytes is not None  # past the image-only return ⇒ audio present
    transcript: TranscriptionResult = transcribe_fn(
        audio_bytes, tenant_id=tenant_id, media_type=audio_media_type,
        consent_check=consent_check,
    )

    # Audio-only → the narration is parsed by the owner-typed extractor (reuse).
    if audio_bytes and not image_bytes:
        entries = extract_owner_typed(
            transcript.transcript_text, tenant_id=tenant_id, now=now,
            client=anthropic_client, consent_check=consent_check,
        )
        return ingest_entries(
            tenant_id, entries, acquired_via="cash_book", now=now,
            park_unattributed=True,
        )

    # Both → vision extraction + transcript → Sonnet multimodal merge.
    vision_results = image_extract_fn(
        image_bytes, tenant_id=tenant_id, target_fields=TARGET_FIELDS,
        acquired_via="cash_book", media_type=image_media_type,
        consent_check=consent_check, client=anthropic_client,
    )
    merged = _merge_photo_and_voice(
        vision_results, transcript.transcript_text,
        client=anthropic_client, model=merge_model,
    )
    logger.info(
        "ingest_cash_book: tenant=%s photo_entries=%d merged_entries=%d",
        tenant_id, len(vision_results), len(merged),
    )
    return ingest_entries(
        tenant_id, merged, acquired_via="cash_book", now=now, park_unattributed=True,
    )


__all__ = ["ingest_cash_book"]
