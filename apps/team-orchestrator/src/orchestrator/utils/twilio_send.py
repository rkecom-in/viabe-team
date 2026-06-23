"""Twilio template-send helper for the direct handlers (VT-3.3c).

Pillar 1: pure send mechanics — no reasoning, no LLM.
Pillar 3: the recipient phone is tokenised in every SendResult; never logged
          or returned in plaintext.
Pillar 7: SendResult honestly reflects the Twilio response — there is no
          hardcoded success. A failed send returns success=False.
Pillar 8: template *content* lives in the Twilio Console + the Meta WABA;
          config/twilio_templates.yaml is a name->content_sid mapping only.

Idempotency: send_template_message is a ``@DBOS.step`` — once it completes,
DBOS checkpoints the SendResult and never re-executes it on workflow replay.
Twilio's Messages API (twilio 9.x) has no idempotency-key parameter, so the
only residual duplicate-send window is a crash after the Twilio call but
before the DBOS checkpoint commits — accepted at Phase 1 scale.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from dbos import DBOS
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from orchestrator.db import tenant_connection
from orchestrator.templates_registry import (
    UnknownTemplateError,
    resolve as _registry_resolve,
)
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)

_TEMPLATES_FILE = Path(__file__).resolve().parents[3] / "config" / "twilio_templates.yaml"


class SendResult(BaseModel):
    """Outcome of one template send. Persisted by callers; PII-safe."""

    success: bool
    message_sid: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempted_at: datetime
    template_name: str
    recipient_phone_token: str  # hash_phone() token — never plaintext


# TemplateNotConfigured is an alias for UnknownTemplateError (D4, VT-163).
# Kept here for back-compat: existing callers that catch TemplateNotConfigured
# continue to work unchanged; the registry raises UnknownTemplateError which IS
# TemplateNotConfigured.
TemplateNotConfigured = UnknownTemplateError


def _templates(*, lang: str = "en") -> dict[str, dict[str, Any]]:
    """Return a {template_name: {content_sid, audience}} dict via the registry.

    Replaces the old @lru_cache yaml loader (D1 migration, VT-163). The
    registry's 60s TTL cache is the single load path. The returned dict
    shape is compatible with callers that read ``template.get("content_sid")``.

    ``lang`` is the language variant to resolve SIDs for; defaults to "en"
    to match the previous implicit behavior.
    """
    # pylint: disable=protected-access
    from orchestrator.templates_registry import _get_cached  # avoid circular at module level
    raw = _get_cached()
    out: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        langs = entry.get("languages") or {}
        content_sid = langs.get(lang)
        out[name] = {
            "content_sid": content_sid,
            "audience": entry.get("audience", ""),
        }
    return out


class _MockTwilioMessages:
    """Mock Twilio messages namespace — logs the would-send and returns a
    fake successful response. NEVER use in production; only when
    ``TEAM_TWILIO_MOCK_MODE=1`` (VT-200 hygiene fix 1).
    """

    @staticmethod
    def create(**kwargs: Any) -> Any:
        safe_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in ("body", "content_variables")
        }
        logger.warning(
            "[TEAM_TWILIO_MOCK_MODE] would-send: %s", safe_kwargs
        )
        return SimpleNamespace(
            sid=f"MK{uuid4().hex[:30]}",
            status="queued",
            error_code=None,
            error_message=None,
        )


class _MockTwilioClient:
    """Mock Twilio REST client used when ``TEAM_TWILIO_MOCK_MODE=1``.

    Surfaces the same ``client.messages.create(...)`` shape the real Twilio
    SDK exposes. Sends never hit the network; each call logs and returns a
    SimpleNamespace shaped like a successful Twilio response so callers
    (``send_template_message`` + canaries) traverse the success branch.
    """

    messages = _MockTwilioMessages()


@lru_cache(maxsize=1)
def _client() -> Client:
    """Build the Twilio REST client from env.

    Lazy (not import-time) so importing this module needs no Twilio creds —
    the CI ``orchestrator`` job has none and tests mock the send. When
    ``TEAM_TWILIO_MOCK_MODE=1``, returns a mock client that logs sends
    instead of dispatching them. Default OFF; the flag is explicit + the
    log line surfaces every send so production drift is loud.
    """
    if os.environ.get("TEAM_TWILIO_MOCK_MODE", "0") == "1":
        logger.warning(
            "TEAM_TWILIO_MOCK_MODE=1 — NOT making real Twilio API calls. "
            "All sends will log and return a mock SID."
        )
        return cast(Client, _MockTwilioClient())  # type: ignore[arg-type]
    return Client(
        os.environ["TEAM_TWILIO_ACCOUNT_SID"],
        os.environ["TEAM_TWILIO_AUTH_TOKEN"],
    )


def get_tenant_whatsapp_number(tenant_id: UUID) -> str | None:
    """Resolve a tenant's own WhatsApp number.

    This is a tenant-scoped read (the tenant's own ``tenants`` row), so it goes
    through ``tenant_connection`` — RLS-enforced under ``app_role`` (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT whatsapp_number FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    return row["whatsapp_number"] if row else None


_WHATSAPP_PREFIX = "whatsapp:"


def _wa(number: str) -> str:
    """Idempotently apply the WhatsApp channel scheme to an E.164 number.

    ``TEAM_TWILIO_FROM_NUMBER`` and recipient numbers are stored/passed as PLAIN E.164 (CL-435).
    Twilio requires ``whatsapp:+…`` on BOTH ``from_`` and ``to`` to route on the WhatsApp channel;
    a raw number misroutes to SMS and fails (VT-399: the welcome to a real signup failed Twilio
    error 21659 because both ends were unprefixed). Idempotent — never double-prefixes.
    """
    return number if number.startswith(_WHATSAPP_PREFIX) else f"{_WHATSAPP_PREFIX}{number}"


@DBOS.step()
def send_template_message(
    tenant_id: UUID,
    template_name: str,
    params: dict[str, Any],
    *,
    recipient_phone: str | None = None,
    language: str = "en",
) -> SendResult:
    """Send a Meta-approved WhatsApp template via Twilio. See the module docstring.

    Raises TemplateNotConfigured (alias: UnknownTemplateError) if template_name
    is unknown. A 4xx Twilio error returns success=False; a 5xx / network error
    is re-raised so the DBOS step retries.

    SID resolution is delegated to templates_registry.resolve() (D1, VT-163).
    ``language`` selects the template's language variant SID; it defaults to "en"
    (the pre-VT-163 implicit behaviour — every existing caller keeps "en"). VT-393:
    the owner welcome honors the owner's preferred_language (team_welcome has EN+HI).
    """
    # Resolve via registry (D1 migration). Raises UnknownTemplateError (== TemplateNotConfigured)
    # for unknown names, UnknownLanguageVariantError for missing language variants.
    try:
        entry = _registry_resolve(template_name, language)
    except UnknownTemplateError:
        raise  # propagates as TemplateNotConfigured (alias)

    recipient = recipient_phone or get_tenant_whatsapp_number(tenant_id)
    if not recipient:
        raise ValueError(
            f"no recipient: tenant {tenant_id} has no whatsapp_number "
            "and no recipient_phone override was given"
        )
    recipient_token = hash_phone(recipient)
    attempted_at = datetime.now(UTC)

    content_sid = entry.content_sid
    if content_sid is None:
        # Stub-pending-approval: the template is configured but its Meta
        # content_sid is not approved yet. No Twilio call (Pillar 7 — honest).
        logger.info(
            "twilio-send: template '%s' has no content_sid (pending approval) -> %s",
            template_name,
            recipient_token,
        )
        return SendResult(
            success=False,
            error_code="template_not_yet_approved",
            error_message=f"template '{template_name}' has no approved content_sid",
            attempted_at=attempted_at,
            template_name=template_name,
            recipient_phone_token=recipient_token,
        )

    try:
        message = _client().messages.create(
            content_sid=content_sid,
            content_variables=json.dumps(params),
            from_=_wa(os.environ["TEAM_TWILIO_FROM_NUMBER"]),
            to=_wa(recipient),
        )
    except TwilioRestException as exc:
        if exc.status is not None and 400 <= exc.status < 500:
            # Permanent (4xx) — surface the failure; the DBOS step does not retry.
            logger.warning(
                "twilio-send: permanent failure template '%s' -> %s (code=%s)",
                template_name,
                recipient_token,
                exc.code,
            )
            return SendResult(
                success=False,
                error_code=str(exc.code),
                error_message=str(exc.msg),
                attempted_at=attempted_at,
                template_name=template_name,
                recipient_phone_token=recipient_token,
            )
        # Transient (5xx / unknown) — re-raise so the DBOS step retries.
        raise

    logger.info(
        "twilio-send: sent template '%s' -> %s (sid=%s)",
        template_name,
        recipient_token,
        message.sid,
    )
    return SendResult(
        success=True,
        message_sid=message.sid,
        attempted_at=attempted_at,
        template_name=template_name,
        recipient_phone_token=recipient_token,
    )


def send_freeform_message(body: str, recipient_phone: str) -> str:
    """Send a free-form WhatsApp message via Twilio (VT-44).

    Parallel to send_template_message but uses Body= instead of content_sid.
    Honors TEAM_TWILIO_MOCK_MODE; never logs the recipient phone in plaintext
    (CL-390 — only the hashed token appears in logs).

    Returns the Twilio message SID (str) on success.
    Raises TwilioRestException on 4xx (permanent) or 5xx (transient).
    The caller (send_whatsapp_message) handles the exception split — this
    function does NOT swallow errors so the caller can record them cleanly.

    Note: NOT a @DBOS.step — the idempotency is handled at the DB layer
    (send_idempotency_keys table) by the standalone tool, not DBOS replay.
    """
    recipient_token = hash_phone(recipient_phone)
    logger.info(
        "twilio-send: freeform -> %s body_len=%d",
        recipient_token,
        len(body),
    )
    message = _client().messages.create(
        body=body,
        from_=_wa(os.environ["TEAM_TWILIO_FROM_NUMBER"]),
        to=_wa(recipient_phone),
    )
    logger.info(
        "twilio-send: freeform sent -> %s (sid=%s)",
        recipient_token,
        message.sid,
    )
    return message.sid
