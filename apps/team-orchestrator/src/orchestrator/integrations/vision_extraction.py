"""VT-52 / VT-6.1 — shared Vision-LLM extraction primitive.

``extract_from_image()`` is the SOLE entry point: an image of a paper artifact
(ledger page, contact list, hand-written customer book) -> structured fields,
each carrying its OWN confidence. All 9 ingestion methods (VT-55..63) call this
one primitive; none re-implement vision.

Consent (CL-390 / CL-342; Cowork VT-52 review 2026-06-01)
    Transmitting the image to Anthropic (a sub-processor) carries the owner's
    CUSTOMERS' PII. Transmission is gated on ``tenants.owner_inputs`` via
    ``_owner_inputs_enabled(tenant_id)`` and is FAIL-CLOSED: no consent ->
    ``ConsentRejectedError`` BEFORE any Anthropic call. Whether owner-level
    consent covers customer PII to a sub-processor under DPDP is VT-269 (Fazal
    production-enablement gate); dev/canary run SYNTHETIC data only (CL-422).

Model (VT-52 row + CL-248/274, superseded by VT-732)
    The SPECIALIST tier (``TEAM_MODEL_SPECIALIST``), resolved per call. The old
    ``config/models.yaml[vision_extraction][VIABE_ENV-slot]`` pin is retired: a
    per-environment tier var already IS the prod/dev split the yaml encoded, and
    two governance surfaces meant a model could be "changed" in the env while the
    yaml quietly kept picking another. The tier must point at a MULTIMODAL model.

Pillars
    P4 retrieve-don't-calculate: an unreadable field -> ``value=None`` with low
    confidence; NEVER a guessed business-type default.
    P8 no-patchwork: malformed model JSON -> raise ``VisionExtractionError``
    (caller triggers the VT-53 clarification flow); NEVER regex-scrub output.
    P3 tenant isolation: ``tenant_id`` is derived from invocation context by the
    caller and only used for the consent check here; it is never taken from
    image content.

Thresholds (criterion 7): single-sourced from
``orchestrator.integrations.field_mapping`` (``_route`` + ``_ASK/_NOTIFY``); this
module adds NO parallel threshold logic.

Retention (CL-330): the raw image is transmitted and dropped — never persisted
by this module.

Tracing (CL-56): the seam's own callbacks record the call on the VT-619 cost
ledger; logfire's LangChain instrumentation covers the span.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.integrations.field_mapping import RoutingDecision, _route

logger = logging.getLogger(__name__)

# Anthropic vision input limits (cost + API): downscale long edge to 1568px
# (the documented sweet-spot above which the API downsamples anyway) and keep
# the encoded payload under 5 MB.
_MAX_LONG_EDGE = 1568
_MAX_BYTES = 5_000_000

# Anthropic vision accepts these media types directly. HEIC is NOT accepted ->
# converted to JPEG in preprocessing.
_DIRECT_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_MAX_OUTPUT_TOKENS = 2048


class ConsentRejectedError(Exception):
    """Raised when ``tenants.owner_inputs`` is not enabled for the tenant.

    Fail-closed: the image is NOT transmitted to Anthropic. The caller must
    surface this as a consent prompt, never retry blindly.
    """


class VisionExtractionError(Exception):
    """Raised when the model returns empty / non-conforming output.

    Per Pillar 8 the caller triggers the VT-53 clarification flow rather than
    regex-repairing the output.
    """


class ImagePreprocessError(VisionExtractionError):
    """Raised on a corrupt / unreadable / unsupported image."""


class ExtractedField(BaseModel):
    """One field the model read off the image, with its own confidence."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1)
    # None = present on the form but unreadable, OR absent. Never a guess (P4).
    value: str | None
    confidence: float = Field(..., ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """Structured extraction + provenance. No raw image retained (CL-330)."""

    model_config = ConfigDict(frozen=True)

    fields: tuple[ExtractedField, ...]
    acquired_via: str
    model: str


def route_field(field: ExtractedField) -> RoutingDecision:
    """Map a field's confidence to the SINGLE-SOURCE routing decision.

    Reuses ``field_mapping._route`` (criterion 7): <0.7 ask_owner /
    0.7-0.85 commit_with_notification / >=0.85 commit_silently.
    """
    return _route(field.confidence)


# VT-732 — the SPECIALIST tier (TEAM_MODEL_SPECIALIST), replacing the
# ``config/models.yaml[vision_extraction][VIABE_ENV-slot]`` pin. The yaml's whole
# job was "a capable model in prod, a cheap one everywhere else", which is what a
# per-ENVIRONMENT tier var already expresses — one governance surface, not two.
# CAPABILITY NOTE: this call sends an IMAGE, so whatever a deployed env points the
# specialist tier at must be MULTIMODAL (gpt-5.6 / gemini / claude are; a text-only
# tier value would fail here and nowhere else).
_VISION_TIER = "specialist"


def _resolve_vision_model() -> str:
    """The concrete model id the vision tier resolves to — for the ExtractionResult's
    ``model`` label and the observability line, never for choosing it here."""
    from orchestrator.llm import resolve_model_id

    return resolve_model_id(_VISION_TIER)


def _image_turn(b64: str, media_type: str, prompt: str) -> Any:
    """One user turn carrying the image + the extraction prompt, as STANDARD langchain content
    blocks. Each provider adapter renders these into its own wire shape (verified against the
    installed pins: anthropic ``source``/base64, OpenAI Responses ``input_image`` data-URL), so the
    image path is provider-portable without this module knowing any provider's format."""
    from langchain_core.messages import HumanMessage

    return HumanMessage(
        content=[
            {"type": "image", "source_type": "base64", "data": b64, "mime_type": media_type},
            {"type": "text", "text": prompt},
        ]
    )


def _default_image_call(tier: str, **kwargs: Any) -> Any:
    """The real transport (lazy import — this module is imported by dep-less paths)."""
    from orchestrator.llm.structured import messages_call

    return messages_call(tier, **kwargs)


def _response_text(resp: Any) -> str:
    """Text of a seam response, tolerating both a plain string and a block list."""
    from orchestrator.llm.structured import response_text

    return response_text(resp)


def _maybe_register_heif() -> bool:
    """Lazily register the HEIF/HEIC opener if pillow-heif is installed.

    Lazy + best-effort (mirrors the weasyprint system-lib pattern): a dev box
    without libheif still imports this module and handles JPEG/PNG/WebP; only the
    HEIC branch needs the optional backend.
    """
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
        return True
    except Exception:  # noqa: BLE001 — optional backend; absence is not fatal
        return False


def _preprocess_image(image_bytes: bytes, media_type_hint: str) -> tuple[bytes, str]:
    """Normalise an arbitrary image to an Anthropic-acceptable payload.

    Handles: HEIC -> JPEG (via optional pillow-heif), oversized (downscale long
    edge to ``_MAX_LONG_EDGE`` and/or re-encode under ``_MAX_BYTES``), and corrupt
    files (Pillow raises -> ``ImagePreprocessError``). Returns (bytes, media_type).
    """
    from PIL import Image, UnidentifiedImageError

    is_heic = media_type_hint in ("image/heic", "image/heif")
    if is_heic:
        _maybe_register_heif()

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ImagePreprocessError(
            f"unreadable/corrupt image (hint={media_type_hint})"
        ) from exc

    fmt = (img.format or "").upper()
    needs_reencode = is_heic or fmt not in ("JPEG", "PNG", "GIF", "WEBP")

    long_edge = max(img.size)
    if long_edge > _MAX_LONG_EDGE:
        scale = _MAX_LONG_EDGE / long_edge
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        )
        needs_reencode = True

    if not needs_reencode and len(image_bytes) <= _MAX_BYTES:
        # Already a direct type, in-bounds — pass through untouched.
        mt = f"image/{fmt.lower()}" if fmt else media_type_hint
        return image_bytes, (mt if mt in _DIRECT_MEDIA_TYPES else "image/jpeg")

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    quality = 90
    img.save(out, format="JPEG", quality=quality)
    # Step quality down until under the byte cap (rare for ledger photos).
    while out.tell() > _MAX_BYTES and quality > 40:
        quality -= 15
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality)
    if out.tell() > _MAX_BYTES:
        raise ImagePreprocessError(
            f"image still {out.tell()} bytes after re-encode (> {_MAX_BYTES})"
        )
    return out.getvalue(), "image/jpeg"


def _build_prompt(target_fields: list[str]) -> str:
    """Render the extraction instruction for the requested field set.

    The versioned base lives at ``agent/prompts/vision_extraction_v1.md``; the
    field list is appended so the model knows exactly what to read + return.
    """
    base_path = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "prompts"
        / "vision_extraction_v1.md"
    )
    base = base_path.read_text(encoding="utf-8")
    fields_block = "\n".join(f"  - {name}" for name in target_fields)
    return f"{base}\n\nFIELDS TO EXTRACT (return one object per field):\n{fields_block}\n"


def extract_from_image(
    image_bytes: bytes,
    *,
    tenant_id: UUID,
    target_fields: list[str],
    acquired_via: str,
    media_type: str = "image/jpeg",
    call: Callable[..., Any] | None = None,
    tier: str | None = None,
    consent_check: Callable[[UUID], bool] | None = None,
) -> ExtractionResult:
    """Extract ``target_fields`` from ``image_bytes`` with per-field confidence.

    Args:
        image_bytes: raw image (jpeg/png/webp/gif/heic). Transmitted + dropped.
        tenant_id: derived from invocation context (P3); used ONLY for the
            consent check, never taken from image content.
        target_fields: canonical field names the caller wants read.
        acquired_via: VT-6 source tag stamped on the result for observability.
        media_type: caller's content-type hint (drives HEIC handling).
        call: optional transport override (tests inject a double; defaults to the
            multi-provider seam's ``messages_call``).
        tier: optional tier override (defaults to the specialist/vision tier).
        consent_check: optional consent predicate (tests/canary inject); defaults
            to ``l0_writer._owner_inputs_enabled`` (reads ``tenants.owner_inputs``).

    Raises:
        ConsentRejectedError: tenant.owner_inputs disabled (fail-closed; no send).
        ImagePreprocessError: corrupt/unsupported/oversized-irrecoverable image.
        VisionExtractionError: empty or non-conforming model output (-> VT-53).
    """
    # 1. CONSENT GATE — fail-closed BEFORE any transmission (CL-390/CL-342).
    if consent_check is None:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        consent_check = _owner_inputs_enabled

    if not consent_check(tenant_id):
        logger.info(
            "vision_extraction: consent absent (tenant=%s) — not transmitting",
            tenant_id,
        )
        raise ConsentRejectedError(
            "tenant.owner_inputs disabled — image NOT transmitted to Anthropic"
        )

    # 2. Preprocess (HEIC convert / downscale / corrupt-detect).
    payload, payload_media_type = _preprocess_image(image_bytes, media_type)

    # 3. Transmit to the vision model through the tier seam (VT-732). The image rides a
    #    STANDARD langchain image block, which each provider adapter translates into its
    #    own wire format (anthropic source/base64, OpenAI input_image data-URL, …).
    resolved_model = _resolve_vision_model()

    b64 = base64.standard_b64encode(payload).decode("ascii")
    resp = (call or _default_image_call)(
        tier or _VISION_TIER,
        messages=[_image_turn(b64, payload_media_type, _build_prompt(target_fields))],
        max_tokens=_MAX_OUTPUT_TOKENS,
        agent="vision_extraction",
        call_site="extract_from_image",
        tenant_id=tenant_id,
    )

    raw = _response_text(resp).strip()
    # Tolerate a ```json fence if the model adds one; do NOT regex-scrub the
    # field VALUES (P8) — this only unwraps an outer fence before json.loads.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if not raw:
        raise VisionExtractionError("vision model returned empty content")

    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionExtractionError(
            f"vision model returned non-JSON: {raw[:200]!r}"
        ) from exc

    rows = parsed.get("fields") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise VisionExtractionError(
            f"vision output missing 'fields' list: {str(parsed)[:200]!r}"
        )

    try:
        fields = tuple(
            ExtractedField(
                name=str(r["name"]),
                value=(None if r.get("value") in (None, "") else str(r["value"])),
                confidence=float(r["confidence"]),
            )
            for r in rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VisionExtractionError(
            f"vision field row failed validation: {str(rows)[:200]!r}"
        ) from exc

    logger.info(
        "vision_extraction: tenant=%s acquired_via=%s fields=%d model=%s",
        tenant_id, acquired_via, len(fields), resolved_model,
    )
    return ExtractionResult(
        fields=fields, acquired_via=acquired_via, model=resolved_model
    )


def _build_entries_prompt(target_fields: list[str]) -> str:
    """Multi-entry variant of the extraction prompt (VT-55 image methods).

    A paper ledger/contact page holds MANY entries; this asks for a JSON array,
    one object per entry, each with the same per-field {value, confidence} shape.
    """
    base_path = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "prompts"
        / "vision_extraction_v1.md"
    )
    base = base_path.read_text(encoding="utf-8")
    fields_block = "\n".join(f"  - {name}" for name in target_fields)
    return (
        f"{base}\n\nThis image contains MULTIPLE entries (e.g. rows in a "
        "handwritten ledger or contact list). Return a single JSON object:\n"
        '{"entries": [{"fields": [{"name": ..., "value": ..., "confidence": ...}]}]}'
        "\nONE entries[] object per row you can read. Each entry's fields use the "
        f"SAME rules above. FIELDS TO EXTRACT per entry:\n{fields_block}\n"
    )


def extract_entries_from_image(
    image_bytes: bytes,
    *,
    tenant_id: UUID,
    target_fields: list[str],
    acquired_via: str,
    media_type: str = "image/jpeg",
    call: Callable[..., Any] | None = None,
    tier: str | None = None,
    consent_check: Callable[[UUID], bool] | None = None,
) -> list[ExtractionResult]:
    """Multi-entry extraction: one ExtractionResult per row in the image.

    Same consent gate (fail-closed), preprocessing, model split, and per-field
    confidence as ``extract_from_image`` — just returns a LIST (a ledger photo is
    many customers). Malformed output → VisionExtractionError (P8; caller routes
    to VT-53). Used by the image ingestion methods (VT-55, kot_pos, cash_book).
    """
    if consent_check is None:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        consent_check = _owner_inputs_enabled
    if not consent_check(tenant_id):
        logger.info(
            "extract_entries_from_image: consent absent (tenant=%s) — not transmitting",
            tenant_id,
        )
        raise ConsentRejectedError(
            "tenant.owner_inputs disabled — image NOT transmitted to Anthropic"
        )

    payload, payload_media_type = _preprocess_image(image_bytes, media_type)
    resolved_model = _resolve_vision_model()
    b64 = base64.standard_b64encode(payload).decode("ascii")
    resp = (call or _default_image_call)(
        tier or _VISION_TIER,
        max_tokens=4096,  # multi-entry → larger budget than the single-record path
        agent="vision_extraction",
        call_site="extract_entries_from_image",
        tenant_id=tenant_id,
        messages=[
            _image_turn(b64, payload_media_type, _build_entries_prompt(target_fields)),
        ],
    )
    raw = _response_text(resp).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if not raw:
        raise VisionExtractionError("vision model returned empty content")
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionExtractionError(
            f"vision model returned non-JSON: {raw[:200]!r}"
        ) from exc

    entries = parsed.get("entries") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        raise VisionExtractionError(
            f"vision output missing 'entries' list: {str(parsed)[:200]!r}"
        )

    results: list[ExtractionResult] = []
    for ent in entries:
        rows = ent.get("fields") if isinstance(ent, dict) else None
        if not isinstance(rows, list):
            raise VisionExtractionError(
                f"entry missing 'fields' list: {str(ent)[:160]!r}"
            )
        try:
            fields = tuple(
                ExtractedField(
                    name=str(r["name"]),
                    value=(None if r.get("value") in (None, "") else str(r["value"])),
                    confidence=float(r["confidence"]),
                )
                for r in rows
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VisionExtractionError(
                f"vision field row failed validation: {str(rows)[:160]!r}"
            ) from exc
        results.append(
            ExtractionResult(fields=fields, acquired_via=acquired_via, model=resolved_model)
        )

    logger.info(
        "extract_entries_from_image: tenant=%s acquired_via=%s entries=%d model=%s",
        tenant_id, acquired_via, len(results), resolved_model,
    )
    return results


__all__ = [
    "ConsentRejectedError",
    "ExtractedField",
    "ExtractionResult",
    "ImagePreprocessError",
    "VisionExtractionError",
    "extract_entries_from_image",
    "extract_from_image",
    "route_field",
]
