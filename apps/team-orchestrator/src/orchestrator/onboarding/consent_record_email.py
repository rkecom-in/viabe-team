"""VT-715 — the DPDP consent-record EMAIL (Fazal: a durable record of the consent, sent to
the tenant, for BOTH capture channels — the web UI form and the WhatsApp I-agree button).

Renders a plain, factual record: what was consented to (DPDP processing + India residency),
the disclosure versions, the capture channel and timestamp, the masked WhatsApp number and
business name. No customer PII (CL-390); the owner's own registered address is the recipient.

Delivery: Resend (the VT-86 monthly-report path's client). Fired fail-soft at both consent
seams; when the tenant has NO owner_email yet (every WhatsApp signup today — the substrate is
new), the record stays PENDING (consent_records.record_email_sent_at IS NULL) and
``send_pending_consent_record`` fires it retroactively the moment an email is captured.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger(__name__)

_FROM_ADDR_ENV = "CONSENT_EMAIL_FROM"  # falls back to ALERT_EMAIL_FROM, then a viabe.ai default


def _mask_phone(phone: str | None) -> str:
    p = str(phone or "").strip()
    return f"•••{p[-4:]}" if len(p) >= 4 else "•••"


def consent_record_html(
    *,
    business_name: str,
    masked_phone: str,
    channel: str,
    dpdpa_version: str,
    residency_version: str,
    consented_at: str,
) -> str:
    """Pure renderer — the factual consent record (EN, plain HTML)."""
    return (
        "<h2>Viabe Team — Consent Record</h2>"
        f"<p>This email is your record of the consent captured for <b>{business_name or 'your business'}</b> "
        f"(WhatsApp {masked_phone}).</p>"
        "<ul>"
        f"<li><b>Data-processing consent (DPDP)</b> — agreed · notice version {dpdpa_version}</li>"
        f"<li><b>India data-residency</b> — agreed · notice version {residency_version}</li>"
        f"<li><b>Captured via</b> — {channel}</li>"
        f"<li><b>Captured at</b> — {consented_at} (UTC)</li>"
        "</ul>"
        "<p>You can pause processing anytime by replying STOP on WhatsApp. "
        "Reply to this email if anything looks wrong.</p>"
    )


def send_consent_record_email(tenant_id: UUID | str, *, channel: str) -> bool:
    """Send (or mark pending) the consent-record email for a tenant. Returns True iff SENT.

    Fail-soft everywhere — a record-email hiccup must never affect the consent itself.
    Idempotent: a stamped record (record_email_sent_at) is never re-sent.
    """
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT t.business_name, t.whatsapp_number, t.owner_email, "
                "       c.dpdpa_version, c.residency_version, c.signed_up_at, c.record_email_sent_at "
                "FROM tenants t JOIN consent_records c ON c.tenant_id = t.id "
                "WHERE t.id = %s ORDER BY c.created_at DESC LIMIT 1",
                (str(tenant_id),),
            ).fetchone()
        if row is None:
            return False
        vals = row if not isinstance(row, dict) else (
            row["business_name"], row["whatsapp_number"], row["owner_email"],
            row["dpdpa_version"], row["residency_version"], row["signed_up_at"],
            row["record_email_sent_at"],
        )
        business_name, phone, owner_email, dpdpa_v, res_v, signed_at, sent_at = vals
        if sent_at is not None:
            return False  # already recorded — never re-send
        if not owner_email:
            logger.info(
                "consent_record_email: PENDING (no owner_email yet) tenant=%s channel=%s",
                tenant_id, channel,
            )
            return False

        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            logger.warning("consent_record_email: RESEND_API_KEY unset — record stays pending")
            return False
        from_addr = (
            os.environ.get(_FROM_ADDR_ENV)
            or os.environ.get("ALERT_EMAIL_FROM")
            or "team@viabe.ai"
        )
        html = consent_record_html(
            business_name=str(business_name or ""),
            masked_phone=_mask_phone(phone),
            channel=channel,
            dpdpa_version=str(dpdpa_v or ""),
            residency_version=str(res_v or ""),
            consented_at=str(signed_at or datetime.now(UTC).isoformat()),
        )
        from orchestrator.alerts.clients import send_resend_email

        ok = asyncio.run(
            send_resend_email(
                api_key, from_addr, str(owner_email),
                "Your Viabe Team consent record", html,
            )
        )
        if ok:
            with tenant_connection(tenant_id) as conn:
                conn.execute(
                    "UPDATE consent_records SET record_email_sent_at = now() "
                    "WHERE tenant_id = %s AND record_email_sent_at IS NULL",
                    (str(tenant_id),),
                )
            logger.info("consent_record_email: SENT tenant=%s channel=%s", tenant_id, channel)
        return bool(ok)
    except Exception:  # noqa: BLE001 — the record email never gates the consent
        logger.warning("consent_record_email failed (fail-soft) tenant=%s", tenant_id, exc_info=True)
        return False


def send_pending_consent_record(tenant_id: UUID | str) -> bool:
    """Retro-fire the record email once an owner_email lands (call from any email-capture
    seam). No-op when already sent or still no email."""
    return send_consent_record_email(tenant_id, channel="email captured post-consent")


__all__ = ["consent_record_html", "send_consent_record_email", "send_pending_consent_record"]
