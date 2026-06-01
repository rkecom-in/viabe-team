"""VT-63 / VT-6 Method 9 — owner-typed natural-language entries (WhatsApp).

The lowest-friction onboarding path: the owner types entries straight into
WhatsApp ("Add Rajesh, 9876543210, yesterday, spent 800" / "नया customer Sunita,
8765432109, परसों आया, 1200"). A Haiku call extracts structured candidates; the
shared ``ingest_entries`` does the routing/dedup/ledger (Pillar 8 — this adapter
is thin coordination, identical posture to ``contacts.py``).

Two-surface scope (Cowork ruling 2026-06-01): this method handles the
IDENTITY/transaction path — name/phone resolvable → dedup_and_merge + (when an
amount is present) a clean attributed customer_ledger_entries row, exactly like
the image methods. Owner-typed input has NO unattributed surface to defer:
something with no name and no phone has nothing to anchor a customer on and is
dropped by ``ingest_entries`` (there is no provider_ref to park in
imported_transactions). So no imported_transactions dependency here.

Parsing is two-phase:
  A. Haiku extraction (owner_typed_v1 prompt) → per-entry {customer_name, phone,
     amount, entry_date} each with its own confidence. The model resolves
     relative dates (TODAY injected) to ISO and rupee phrases ("1.2k", "₹800",
     "१२००") to a plain rupee integer string — so Phase B reuses the existing
     deterministic parsers (clarifying_flow.parse_amount_to_paise via
     ingest_entries, _image_adapter._parse_date) with no new date/amount grammar.
  B. Deterministic post-processing — phone normalised to E.164 via the shared
     contacts._normalize_phone (cross-method dedup), confidence blended with the
     model's so a foreign/odd number still routes to the clarifying flow.

Consent (CL-390/CL-342): the typed message carries the owner's CUSTOMERS' PII to
Anthropic (a sub-processor), same as vision. Gated FAIL-CLOSED on
``tenants.owner_inputs`` before any transmission. Dev/canary = SYNTHETIC only
(CL-422). PII never logged (CL-390); counts only.

Pillars: P4 missing field → null, never invented (the prompt enforces this; a
malformed model reply raises rather than being regex-repaired). P7 the owner's
typed input is canonical. P8 shared dedup/clarify/phone-normalisation.

Out of scope (follow-ups, logged on the PR):
  - VT-5.11 ``classify_owner_message`` v2 bump adding the ``owner_typed_entry``
    intent that triggers this method (Type-1 governance; ships alongside).
  - The owner-facing WhatsApp confirmation SEND is the owner-surface/webhook
    layer (VT-9.4) — same posture as VT-55/56. ``build_confirmation`` provides
    the masked-phone wording artifact (Fazal reviews wording) for that layer.
  - The 0.6 owner-typed clarification leniency (vs 0.7 image): NOT forked into
    the single-source ``field_mapping._route`` (criterion 7 / Pillar 8). Tracked
    as a follow-up; today owner-typed uses the same shared threshold.
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
    IngestionSummary,
    ingest_entries,
)
from orchestrator.integrations.methods.contacts import _normalize_phone
from orchestrator.integrations.vision_extraction import (
    ExtractedField,
    ExtractionResult,
)

logger = logging.getLogger(__name__)

# config/models.yaml — parents: [0]=methods [1]=integrations [2]=orchestrator
# [3]=src [4]=team-orchestrator.
_MODELS_YAML = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
_PROMPT = (
    Path(__file__).resolve().parents[2]
    / "agent"
    / "prompts"
    / "owner_typed_v1.md"
)
_MAX_OUTPUT_TOKENS = 2048

# The canonical owner-typed field set — same contract the image methods use
# (_image_adapter.TARGET_FIELDS), so ingest_entries routes/commits identically.
_FIELD_NAMES = ("customer_name", "phone", "amount", "entry_date")


class OwnerTypedExtractionError(Exception):
    """Raised when the model returns empty / non-conforming output.

    Per Pillar 8 the caller surfaces the "couldn't parse that" reply rather than
    regex-repairing the output.
    """


def _resolve_owner_typed_model() -> str:
    """Model id for ``owner_typed_extraction`` per ``VIABE_ENV`` (Haiku both slots)."""
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["owner_typed_extraction"][slot])


def _build_prompt(now: datetime) -> str:
    """Render the extraction instruction with TODAY injected for date resolution."""
    base = _PROMPT.read_text(encoding="utf-8")
    return base.replace("{today}", now.date().isoformat())


def _fields_from_rows(rows: list[dict[str, Any]]) -> tuple[ExtractedField, ...]:
    """Build ExtractedFields from one entry's model rows, normalising phone.

    Phone is reformatted to E.164 via the shared contacts normaliser (cross-method
    dedup) and its confidence blended with the model's (min) so a foreign/odd
    number still routes to the clarifying flow. Junk phone → value None.
    """
    fields: list[ExtractedField] = []
    for r in rows:
        name = str(r["name"])
        raw_value = r.get("value")
        value = None if raw_value in (None, "") else str(raw_value)
        conf = float(r["confidence"])
        if name == "phone" and value is not None:
            e164, norm_conf = _normalize_phone(value)
            value = e164
            conf = min(conf, norm_conf) if e164 is not None else 0.0
        fields.append(ExtractedField(name=name, value=value, confidence=conf))
    return tuple(fields)


def _parse_entries(raw: str, model: str) -> list[ExtractionResult]:
    """Parse the model's JSON reply into ExtractionResults (P8: no regex-scrub)."""
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if not raw:
        raise OwnerTypedExtractionError("owner_typed model returned empty content")
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OwnerTypedExtractionError(
            f"owner_typed model returned non-JSON: {raw[:200]!r}"
        ) from exc

    entries = parsed.get("entries") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        raise OwnerTypedExtractionError(
            f"owner_typed output missing 'entries' list: {str(parsed)[:200]!r}"
        )

    results: list[ExtractionResult] = []
    for ent in entries:
        rows = ent.get("fields") if isinstance(ent, dict) else None
        if not isinstance(rows, list):
            raise OwnerTypedExtractionError(
                f"entry missing 'fields' list: {str(ent)[:160]!r}"
            )
        try:
            fields = _fields_from_rows(rows)
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnerTypedExtractionError(
                f"owner_typed field row failed validation: {str(rows)[:160]!r}"
            ) from exc
        results.append(
            ExtractionResult(fields=fields, acquired_via="owner_typed", model=model)
        )
    return results


def extract_owner_typed(
    message_body: str,
    *,
    tenant_id: UUID | str,
    now: datetime | None = None,
    client: Anthropic | None = None,
    model: str | None = None,
    consent_check: Callable[[UUID], bool] | None = None,
) -> list[ExtractionResult]:
    """Extract customer entries from a typed owner message (consent-gated).

    Raises ``ConsentRejectedError`` (fail-closed, no transmission) if
    ``tenants.owner_inputs`` is disabled, ``OwnerTypedExtractionError`` on
    empty/non-conforming model output (→ owner re-ask). ``client``/``model``/
    ``consent_check`` are injectable for tests + canary.
    """
    now = now or datetime.now(UTC)

    # CONSENT GATE — fail-closed BEFORE any transmission (CL-390/CL-342).
    if consent_check is None:
        from orchestrator.memory.l0_writer import _owner_inputs_enabled

        consent_check = _owner_inputs_enabled
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    if not consent_check(tid):
        from orchestrator.integrations.vision_extraction import ConsentRejectedError

        logger.info(
            "owner_typed: consent absent (tenant=%s) — not transmitting", tenant_id
        )
        raise ConsentRejectedError(
            "tenant.owner_inputs disabled — message NOT transmitted to Anthropic"
        )

    if client is None:
        client = Anthropic()
    resolved_model = model or _resolve_owner_typed_model()
    resp = client.messages.create(
        model=resolved_model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_prompt(now)},
                    {"type": "text", "text": message_body},
                ],
            }
        ],
    )
    text = "".join(
        getattr(b, "text", "") for b in resp.content
        if getattr(b, "type", "") == "text"
    ).strip()
    results = _parse_entries(text, resolved_model)
    logger.info(
        "owner_typed: tenant=%s entries=%d model=%s",
        tenant_id, len(results), resolved_model,
    )
    return results


def ingest_owner_typed(
    tenant_id: UUID | str,
    message_body: str,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
    client: Anthropic | None = None,
    model: str | None = None,
    consent_check: Callable[[UUID], bool] | None = None,
) -> IngestionSummary:
    """Parse a typed owner message → dedup + commit identity/ledger rows.

    Thin coordination over ``extract_owner_typed`` + the shared ``ingest_entries``
    (acquired_via='owner_typed'). tenant_id from invocation context (P3). run_id
    accepted for telemetry parity (unused here). Returns counts only (no PII).
    """
    now = now or datetime.now(UTC)
    results = extract_owner_typed(
        message_body, tenant_id=tenant_id, now=now,
        client=client, model=model, consent_check=consent_check,
    )
    return ingest_entries(tenant_id, results, acquired_via="owner_typed", now=now)


def _mask_phone(phone_e164: str | None) -> str | None:
    """Mask all but the last 4 digits (owner-confidence without overexposure)."""
    if not phone_e164:
        return None
    tail = phone_e164[-4:]
    return "•" * max(0, len(phone_e164) - 4) + tail


def build_confirmation(
    *,
    customer_name: str | None,
    phone_e164: str | None,
    amount_paise: int | None,
    entry_date: str | None,
) -> str:
    """Owner-facing confirmation wording (masked phone). Pure — no send, no log.

    The owner-surface/webhook layer (VT-9.4) sends this; Fazal reviews the wording
    + the masking gesture (VT-63 acceptance). NEVER logged (it carries a name).
    """
    who = customer_name or "customer"
    masked = _mask_phone(phone_e164)
    bits: list[str] = []
    if masked:
        bits.append(masked)
    if amount_paise is not None:
        bits.append(f"₹{amount_paise // 100}")
    if entry_date:
        bits.append(entry_date)
    detail = " — " + ", ".join(bits) if bits else ""
    return f"Added {who}{detail}."


PARSE_FAILURE_REPLY = (
    "I couldn't quite parse that. Could you try again like "
    "\"Add [name], phone [number], visited [date], spent ₹[amount]\"?"
)


__all__ = [
    "PARSE_FAILURE_REPLY",
    "IngestionSummary",
    "OwnerTypedExtractionError",
    "build_confirmation",
    "extract_owner_typed",
    "ingest_owner_typed",
]
