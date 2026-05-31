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

Model (VT-52 row + CL-248/274)
    Sonnet 4.6 production / Haiku 4.5 canary, via
    ``config/models.yaml[vision_extraction][slot]`` resolved by ``VIABE_ENV``
    (same convention as sales_recovery / self_evaluate / owner_input_classifier).

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

Tracing (CL-56): ``logfire.instrument_anthropic()`` (configured at startup)
auto-instruments the SDK call; no extra wiring needed here.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml
from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.integrations.field_mapping import RoutingDecision, _route

logger = logging.getLogger(__name__)

# config/models.yaml — apps/team-orchestrator/config/models.yaml.
# file = src/orchestrator/integrations/vision_extraction.py
# parents: [0]=integrations [1]=orchestrator [2]=src [3]=team-orchestrator
_MODELS_YAML = Path(__file__).resolve().parents[3] / "config" / "models.yaml"

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


def _resolve_vision_model() -> str:
    """Model id for ``vision_extraction`` per ``VIABE_ENV`` (prod Sonnet / else Haiku).

    Unset/test/dev/canary -> ``test`` slot (Haiku); never silently Sonnet/Opus in
    a non-production environment.
    """
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["vision_extraction"][slot])


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
    client: Anthropic | None = None,
    model: str | None = None,
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
        client: optional Anthropic client (tests inject a mock).
        model: optional model override (else resolved by VIABE_ENV).
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

    # 3. Transmit to the vision model. Raw anthropic SDK (image content block),
    #    mirroring classify_owner_message's standalone-tool pattern.
    if client is None:
        client = Anthropic()
    resolved_model = model or _resolve_vision_model()

    b64 = base64.standard_b64encode(payload).decode("ascii")
    resp = client.messages.create(
        model=resolved_model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": payload_media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _build_prompt(target_fields)},
                ],
            }
        ],
    )

    text_blocks = [
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ]
    raw = "".join(text_blocks).strip()
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


__all__ = [
    "ConsentRejectedError",
    "ExtractedField",
    "ExtractionResult",
    "ImagePreprocessError",
    "VisionExtractionError",
    "extract_from_image",
    "route_field",
]
