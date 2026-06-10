"""VT-335 — owner template-error report handler.

The owner reports that a message we sent was wrong/broken. We log a report (the owner's
free text — PII, may name a customer), attach the most-recent outbound template_id (7d),
and alert Fazal. Pure of the send (the caller delivers the ack).

template_error_reports is RLS+FORCE; the insert is service-role (get_pool, BYPASSRLS) with
an explicit tenant_id predicate. The Fazal alert carries NO complaint text (PII stays at
rest; Fazal opens the report by id) — CL-390.
"""

from __future__ import annotations

import logging
from typing import NamedTuple
from uuid import UUID

logger = logging.getLogger(__name__)

_MAX_COMPLAINT = 2000


class TemplateErrorResult(NamedTuple):
    report_id: UUID | None
    recent_template_id: str | None
    response_text: str


def _recent_template_id(tenant_id: UUID | str) -> str | None:
    """The most-recent outbound campaign's template_id within 7 days (or None)."""
    from orchestrator.db.wrappers import CampaignsWrapper

    try:
        rows = CampaignsWrapper().list_recent_with_responses(tenant_id, days_back=7, limit=1)
    except Exception:
        logger.exception("template_error: recent-template lookup failed tenant=%s", tenant_id)
        return None
    return (rows[0].get("template_id") if rows else None) or None


def _alert_fazal_safe(tenant_id: UUID | str, recent_tid: str | None, report_id: UUID | None) -> None:
    """Best-effort Telegram alert — NO complaint text (PII stays at rest)."""
    try:
        from orchestrator.alerts.clients import alert_fazal as _alert_fazal

        _alert_fazal(
            f"⚠️ Template-error report (VT-335)\n"
            f"tenant={tenant_id}\nrecent_template={recent_tid}\nreport={report_id}"
        )
    except Exception:
        logger.exception("template_error: Fazal alert failed tenant=%s", tenant_id)


def handle_template_error(tenant_id: UUID | str, body: str) -> TemplateErrorResult:
    """Log a template-error report + alert Fazal. Never raises into the dispatch path."""
    from orchestrator.graph import get_pool

    complaint = (body or "").strip()[:_MAX_COMPLAINT]
    recent_tid = _recent_template_id(tenant_id)

    report_id: UUID | None = None
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO template_error_reports "
                "(tenant_id, owner_complaint, recent_template_id) VALUES (%s, %s, %s) "
                "RETURNING id",
                (str(tenant_id), complaint, recent_tid),
            ).fetchone()
            report_id = UUID(str(row["id"])) if row else None
    except Exception:
        logger.exception("template_error: report insert failed tenant=%s", tenant_id)

    _alert_fazal_safe(tenant_id, recent_tid, report_id)
    return TemplateErrorResult(
        report_id,
        recent_tid,
        "Thanks for flagging that — Fazal will review the message and follow up.",
    )
