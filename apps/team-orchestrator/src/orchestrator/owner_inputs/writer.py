"""Owner-input extraction writer (VT-146).

Reads an inbound WhatsApp owner message body from the request-scoped
``WebhookEvent`` and classifies it into structured
``intent / segment / occasion`` via an Anthropic Haiku call. The
derived row is written to ``owner_inputs`` via the tenant-scoped
connection helper. Raw body is NEVER persisted by this writer — the
table has no body column (migration ``020_owner_inputs.sql``) and the
classifier's input is dropped after the Messages-API round-trip
returns.

Privacy posture (Fazal brief, VT-146):

- **RETAINED**: the table holds derived ``intent / segment / occasion``
  only.
- **TRANSMITTED**: the body text is sent to Anthropic for classification
  on each inbound message. WhatsApp BSP + Anthropic Commercial Terms
  permit this; the executed DPA + Zero Data Retention request gate
  MERGE (Ship Gate).
- **DERIVED**: the classification fields are what downstream code reads
  (Composer -> agent).

Failure mode: classification errors must NOT break the inbound webhook
pipeline. Callers wrap the writer in best-effort try/except — a
classification miss leaves the pipeline running and is observable via
the routed ``FailureRecord`` path. See ``run_extraction_for_event`` for
the recommended call site.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import yaml
from anthropic import Anthropic

from orchestrator._tenant_guard import assert_tenant_scoped
from orchestrator.db import tenant_connection
from orchestrator.types import WebhookEvent

_logger = logging.getLogger(__name__)

_MODELS_YAML = (
    Path(__file__).resolve().parents[3] / "config" / "models.yaml"
)

# VT-146 classifier — Haiku in both slots (production parity with the
# canary). Keep the per-turn output cap small: the classifier emits a
# tiny JSON object and nothing else. Generous enough that the model
# never truncates the JSON; tight enough that a runaway emit is bounded.
_CLASSIFIER_MAX_OUTPUT_TOKENS = 256

# Classification system prompt (v1.0). The schema is locked into the
# prompt body; the writer parses the response as JSON. Strict
# instruction to emit JSON only — no preamble, no fence — matches the
# parse contract below. ``unclassified`` is a sentinel intent reserved
# for the failure path; the model is told NOT to emit it.
_SYSTEM_PROMPT = """\
You are an intent classifier for inbound WhatsApp messages from small-
business owners using a sales-recovery service. Classify the message
into a strict JSON object with these three keys:

  - "intent": one of ["winback", "campaign_request", "feedback",
    "exclusion_request", "question", "other"]. Required, non-empty.
  - "segment": short string naming the customer cohort the owner has in
    mind, OR null if the message does not specify one.
  - "occasion": short string naming the festival / season / event the
    owner references, OR null if no occasion is mentioned.

Emit ONLY the JSON object. No prose, no markdown fence, no preamble.
The keys "segment" and "occasion" must be present in the JSON (as
strings or as the JSON literal null). Do NOT emit "unclassified" —
that value is reserved for the writer's failure path."""

# Recognised markdown fence — narrow, same shape the sales_recovery
# parser tolerates. The model is told not to fence; tolerate one
# wrapper anyway because borderline models intermittently still emit it.
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

_ALLOWED_INTENTS = frozenset(
    {
        "winback",
        "campaign_request",
        "feedback",
        "exclusion_request",
        "question",
        "other",
    }
)

_UNCLASSIFIED_SENTINEL = "unclassified"


@dataclass(frozen=True, slots=True)
class OwnerInputClassification:
    """The structured-extraction output. ``intent`` is required and
    constrained to ``_ALLOWED_INTENTS`` plus the ``unclassified``
    sentinel produced by the failure path. ``segment`` / ``occasion``
    are free-text labels or None."""

    intent: str
    segment: str | None = None
    occasion: str | None = None


def _resolve_classifier_model() -> str:
    """Return the classifier model id per ``VIABE_ENV``.

    Per ``models.yaml``, both slots resolve to Haiku for VT-146 — the
    helper still respects the same env split so the resolver shape
    matches the sales_recovery / self_evaluate pattern and a future
    slot demotion does not require touching this writer.
    """
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config["owner_input_classifier"][slot])


def classify_message(
    body: str,
    *,
    client: Anthropic | None = None,
) -> OwnerInputClassification:
    """Classify an owner message body into ``OwnerInputClassification``.

    Sends ``body`` to the Anthropic Messages API (Haiku per
    ``models.yaml``) with the v1.0 classification system prompt;
    parses the JSON response. On parse failure or an unknown
    discriminator, returns ``intent='unclassified'`` rather than
    raising — the writer's contract is best-effort, the inbound
    pipeline must not fail.

    ``client``: dependency injection for tests; the production path
    constructs ``Anthropic()`` per call (cheap; the SDK keeps the HTTP
    pool internally).
    """
    if not body or not body.strip():
        return OwnerInputClassification(intent=_UNCLASSIFIED_SENTINEL)

    sdk = client if client is not None else Anthropic()
    try:
        response = sdk.messages.create(
            model=_resolve_classifier_model(),
            max_tokens=_CLASSIFIER_MAX_OUTPUT_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": body}],
        )
    except Exception:  # noqa: BLE001 — best-effort observability seam
        _logger.warning(
            "owner_input_classifier: SDK call failed", exc_info=True
        )
        return OwnerInputClassification(intent=_UNCLASSIFIED_SENTINEL)

    text = _extract_text(response)
    parsed = _parse_classifier_json(text)
    if parsed is None:
        _logger.warning(
            "owner_input_classifier: response did not parse as JSON; "
            "first 200 chars: %s",
            text.strip()[:200],
        )
        return OwnerInputClassification(intent=_UNCLASSIFIED_SENTINEL)

    intent_value = parsed.get("intent")
    if not isinstance(intent_value, str) or intent_value not in _ALLOWED_INTENTS:
        _logger.warning(
            "owner_input_classifier: unknown intent %r — coercing to "
            "unclassified",
            intent_value,
        )
        return OwnerInputClassification(intent=_UNCLASSIFIED_SENTINEL)

    segment_value = parsed.get("segment")
    occasion_value = parsed.get("occasion")
    return OwnerInputClassification(
        intent=intent_value,
        segment=_coerce_optional_str(segment_value),
        occasion=_coerce_optional_str(occasion_value),
    )


def write_owner_input(
    tenant_id: UUID,
    *,
    run_id: UUID | None,
    message_sid: str | None,
    classification: OwnerInputClassification,
) -> UUID:
    """INSERT one ``owner_inputs`` row carrying only derived fields.

    Tenant-scoped (RLS + GUC via ``tenant_connection``); the INSERT
    payload is checked via ``assert_tenant_scoped`` post-write to keep
    the belt-and-braces guard parallel to the rest of the codebase.
    Returns the new row's ``id``.

    Raw body is NOT a parameter to this function. The schema has no
    body column. The classifier ran before the writer was called; this
    function knows nothing about the original message text. Locking
    that surface is the whole point of the VT-146 derived-only design.
    """
    new_id = uuid4()
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO owner_inputs "
            "(id, tenant_id, run_id, message_sid, intent, segment, occasion) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                str(new_id),
                str(tenant_id),
                str(run_id) if run_id is not None else None,
                message_sid,
                classification.intent,
                classification.segment,
                classification.occasion,
            ),
        )
        # Belt-and-braces over RLS: re-read the just-written row + assert
        # its tenant_id matches the expected value before returning.
        raw = conn.execute(
            "SELECT id, tenant_id FROM owner_inputs WHERE id = %s",
            (str(new_id),),
        ).fetchall()
        rows = cast("list[dict[str, Any]]", raw)
    assert_tenant_scoped(rows, tenant_id)
    return new_id


def run_extraction_for_event(
    tenant_id: UUID,
    run_id: UUID,
    event: WebhookEvent,
    *,
    client: Anthropic | None = None,
) -> UUID | None:
    """Best-effort extraction entry point — call from the inbound flow.

    The inbound webhook pipeline calls this AFTER ``record_webhook_received``
    and BEFORE ``pre_filter``. Wrapping is INSIDE this function (so the
    caller does not need its own try/except) — any classification or
    write failure returns ``None`` and logs, but never re-raises into
    the pipeline. The owner ingress path stays resilient regardless of
    classifier or DB issues.

    Inputs:
        ``event.body`` — request-scoped plaintext, read here and never
        persisted. (VT-144 closed the prior body-retention surface in
        ``pipeline_runs.trigger_payload`` / ``pipeline_steps.input_envelope``;
        this writer reads body from the same request-scoped event the
        pre_filter already consumes.)
    """
    if event.message_type == "status_callback":
        # Status callbacks have no owner message to classify.
        return None
    if not event.body or not event.body.strip():
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # No classifier configured for this environment — skip cleanly
        # rather than piling up ``unclassified`` rows. Production sets
        # the key; CI's orchestrator job intentionally does not (the
        # real-API canary supplies it under its own env-gate). Keeps
        # the inbound webhook tests free of writer side effects.
        return None

    try:
        classification = classify_message(event.body, client=client)
        return write_owner_input(
            tenant_id,
            run_id=run_id,
            message_sid=event.twilio_message_sid,
            classification=classification,
        )
    except Exception:  # noqa: BLE001 — observability cannot break recovery
        _logger.exception(
            "owner_input writer: extraction failed for run_id=%s; "
            "inbound pipeline continues",
            run_id,
        )
        return None


def _extract_text(response: Any) -> str:
    """Concatenate every TextBlock's text from a Messages API response."""
    blocks = getattr(response, "content", []) or []
    out: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _parse_classifier_json(text: str) -> dict[str, Any] | None:
    """Tolerate one level of markdown-fence wrapping; reject anything else."""
    text = text.strip()
    if not text:
        return None
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match is not None:
        text = fence_match.group("body").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _coerce_optional_str(value: Any) -> str | None:
    """Accept ``None`` / strings; reject anything else (-> None)."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
