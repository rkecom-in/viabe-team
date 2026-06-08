"""VT-86 — monthly report orchestration entrypoint (D8 trigger wiring).

`run_monthly_report(tenant_id, year_month, conn)` ties the pieces together for
one tenant: generate (deterministic SQL) → render PDF → store → email → persist
the `monthly_reports` row + delivery state. Called per eligible tenant by the
monthly-impact scheduled trigger (scheduled_triggers.py).

Every external step (render/store/send) is an INJECTABLE callable defaulting to
the real implementation, so the orchestration logic — skip handling, row
upsert, email-success vs failure-count — is unit-testable with fakes (no
weasyprint/Supabase/Resend on dev), while production wires the real ones.

Pillar 1: the data is deterministic SQL (generate_monthly_report). Pillar 7:
skipped/zero-activity tenants are handled honestly (skip is recorded as a
no-op, not hidden).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from orchestrator.owner_surface.monthly_report import (
    MonthlyReport,
    generate_monthly_report,
)
from orchestrator.owner_surface.monthly_report_email import send_report_email
from orchestrator.owner_surface.monthly_report_pdf import render_report_pdf
from orchestrator.owner_surface.report_storage import store_report_pdf

logger = logging.getLogger(__name__)

_PORTAL_BASE = os.environ.get("OWNER_PORTAL_URL", "https://viabe.ai/team")


def _send_sync(
    report: MonthlyReport, pdf: bytes, *, to_addr: str, portal_url: str
) -> bool:
    """Sync wrapper over the async Resend send (the trigger body is sync)."""
    from orchestrator.alerts.email_senders import sender_from

    api_key = os.environ.get("RESEND_API_KEY", "")
    from_addr = sender_from("alerts")  # VT-113: canonical registry (D6 ops@ via RESEND_FROM_EMAIL override)
    return asyncio.run(
        send_report_email(
            report, pdf, to_addr=to_addr, portal_url=portal_url,
            api_key=api_key, from_addr=from_addr,
        )
    )


def run_monthly_report(
    tenant_id: str,
    year_month: str,
    *,
    conn: Any,
    owner_email: str | None,
    generate: Callable[..., MonthlyReport | None] = generate_monthly_report,
    render: Callable[[MonthlyReport], bytes] = render_report_pdf,
    store: Callable[..., str] = store_report_pdf,
    send: Callable[..., bool] = _send_sync,
    portal_base_url: str | None = None,
) -> dict[str, Any]:
    """Produce + deliver + persist one tenant's monthly report.

    Returns a result dict: ``{"status": "skipped"|"generated", ...}``. The
    monthly_reports row is upserted (UNIQUE tenant+month) so a retry re-runs
    cleanly. Email failure does NOT abort — it records email_failure_count so
    the trigger can retry; the PDF is still stored + the row persisted.
    """
    report = generate(tenant_id, year_month, conn=conn)
    if report is None:
        logger.info("monthly_report: tenant=%s month=%s SKIPPED", tenant_id, year_month)
        return {"status": "skipped", "tenant_id": tenant_id, "year_month": year_month}

    pdf = render(report)
    storage_path = store(tenant_id, year_month, pdf)

    portal_url = f"{portal_base_url or _PORTAL_BASE}/reports/{year_month}"
    email_ok = False
    email_attempted = bool(owner_email)
    if email_attempted:
        try:
            email_ok = send(report, pdf, to_addr=owner_email, portal_url=portal_url)
        except Exception:  # delivery must not abort persistence
            logger.exception("monthly_report: email send raised tenant=%s", tenant_id)
            email_ok = False
    # A missing owner_email is a data gap, NOT a delivery failure — don't bump
    # the retry counter (nothing to retry). Only an attempted-and-failed send
    # increments it so the trigger's retry path picks it up.
    bump_fail = email_attempted and not email_ok

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO monthly_reports
                (tenant_id, year_month, pdf_storage_path, arrr_paise,
                 email_sent_at, email_failure_count)
            VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END,
                    CASE WHEN %s THEN 1 ELSE 0 END)
            ON CONFLICT (tenant_id, year_month) DO UPDATE SET
                pdf_storage_path = EXCLUDED.pdf_storage_path,
                arrr_paise = EXCLUDED.arrr_paise,
                email_sent_at = CASE WHEN %s THEN now()
                                     ELSE monthly_reports.email_sent_at END,
                email_failure_count = CASE WHEN %s
                                           THEN monthly_reports.email_failure_count + 1
                                           ELSE monthly_reports.email_failure_count END
            """,
            (tenant_id, year_month, storage_path, report.arrr_paise,
             email_ok, bump_fail, email_ok, bump_fail),
        )

    return {
        "status": "generated",
        "tenant_id": tenant_id,
        "year_month": year_month,
        "storage_path": storage_path,
        "arrr_paise": report.arrr_paise,
        "email_sent": email_ok,
    }


__all__ = ["run_monthly_report"]
