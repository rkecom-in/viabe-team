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
from typing import Any
from uuid import UUID

import yaml
from dbos import DBOS
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from orchestrator.db import tenant_connection
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


class TemplateNotConfigured(ValueError):
    """Raised when template_name is not present in twilio_templates.yaml."""


@lru_cache(maxsize=1)
def _templates() -> dict[str, dict[str, Any]]:
    """Load + cache the name -> {content_sid, audience} template map."""
    data = yaml.safe_load(_TEMPLATES_FILE.read_text())
    return dict(data or {})


@lru_cache(maxsize=1)
def _client() -> Client:
    """Build the Twilio REST client from env.

    Lazy (not import-time) so importing this module needs no Twilio creds —
    the CI ``orchestrator`` job has none and tests mock the send.
    """
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


@DBOS.step()
def send_template_message(
    tenant_id: UUID,
    template_name: str,
    params: dict[str, Any],
    *,
    recipient_phone: str | None = None,
) -> SendResult:
    """Send a Meta-approved WhatsApp template via Twilio. See the module docstring.

    Raises TemplateNotConfigured if template_name is unknown. A 4xx Twilio
    error returns success=False; a 5xx / network error is re-raised so the
    DBOS step retries.
    """
    template = _templates().get(template_name)
    if template is None:
        raise TemplateNotConfigured(
            f"template '{template_name}' not in twilio_templates.yaml"
        )

    recipient = recipient_phone or get_tenant_whatsapp_number(tenant_id)
    if not recipient:
        raise ValueError(
            f"no recipient: tenant {tenant_id} has no whatsapp_number "
            "and no recipient_phone override was given"
        )
    recipient_token = hash_phone(recipient)
    attempted_at = datetime.now(UTC)

    content_sid = template.get("content_sid")
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
            from_=os.environ["TEAM_TWILIO_FROM_NUMBER"],
            to=recipient,
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
