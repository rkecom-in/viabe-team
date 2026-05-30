"""VT-86 — monthly report email delivery (Resend, D6).

Builds the EN/HI owner email (2-paragraph summary + portal link + PDF
attachment) and sends it via the shared Resend client (reuse — Pillar 8). The
from-address is the verified RESEND_FROM_EMAIL (D6 ruling: reuse ops@, not a
new noreply@ DMARC dependency).

Body builders are pure + tested. The send wraps an injectable send_fn (tests
pass a mock; the canary does a real send to a TEST recipient behind an env flag
— NEVER a real customer, CL-422). No customer PII in the email — business name
+ aggregate figures only (CL-390).
"""

from __future__ import annotations

import base64
import html
from collections.abc import Awaitable, Callable

from orchestrator.alerts.clients import send_resend_email
from orchestrator.owner_surface.monthly_report import MonthlyReport
from orchestrator.owner_surface.monthly_report_pdf import money_inr

_MONTHS_EN = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _period_label(year_month: str) -> str:
    year, month = year_month.split("-")
    return f"{_MONTHS_EN[int(month)]} {year}"


def report_subject(report: MonthlyReport) -> str:
    period = _period_label(report.year_month)
    if report.language == "hi":
        return f"आपकी Viabe Team प्रभाव रिपोर्ट — {period}"
    return f"Your Viabe Team Impact Report — {period}"


def report_email_html(report: MonthlyReport, portal_url: str) -> str:
    """Pure 2-paragraph EN/HI email body + portal link. No PII (CL-390)."""
    period = _period_label(report.year_month)
    arrr = money_inr(report.arrr_paise)
    # Escape the owner-set business_name before HTML interpolation (it is the
    # only free-text, owner-controlled field in this body) — consistent with
    # the PDF renderer's _esc(). Prevents HTML/script injection into the email.
    biz = html.escape(report.business_name)
    if report.language == "hi":
        p1 = (f"नमस्ते {biz}, {period} की आपकी प्रभाव रिपोर्ट तैयार है। "
              f"इस महीने जिम्मेदार राजस्व: <b>{arrr}</b>, भेजे गए अभियान: "
              f"{report.campaigns_sent}, नए ग्राहक: {report.customers_added}।")
        if report.zero_arrr:
            p1 += " इस महीने कोई जिम्मेदार राजस्व नहीं रहा — पूरी जानकारी रिपोर्ट में है।"
        p2 = (f'पूरी रिपोर्ट संलग्न PDF में है। पोर्टल पर देखें: '
              f'<a href="{portal_url}">{portal_url}</a>')
    else:
        p1 = (f"Hi {biz}, your impact report for {period} is ready. "
              f"Attributed revenue this month: <b>{arrr}</b>, campaigns sent: "
              f"{report.campaigns_sent}, new customers: {report.customers_added}.")
        if report.zero_arrr:
            p1 += " No attributed revenue this month — the full picture is in the report."
        p2 = (f'The full report is attached as a PDF. View it in your portal: '
              f'<a href="{portal_url}">{portal_url}</a>')
    return (f'<div style="font-family:sans-serif;font-size:14px;line-height:1.5;">'
            f"<p>{p1}</p><p>{p2}</p></div>")


def pdf_attachment(report: MonthlyReport, pdf_bytes: bytes) -> dict:
    """Resend attachment dict: base64 content + a stable filename."""
    return {
        "filename": f"viabe-impact-{report.year_month}.pdf",
        "content": base64.b64encode(pdf_bytes).decode("ascii"),
    }


async def send_report_email(
    report: MonthlyReport,
    pdf_bytes: bytes,
    *,
    to_addr: str,
    portal_url: str,
    api_key: str,
    from_addr: str,
    send_fn: Callable[..., Awaitable[bool]] = send_resend_email,
) -> bool:
    """Send the monthly report email with the PDF attached. Returns True on a
    2xx from Resend. `send_fn` is injectable for tests/canary."""
    return await send_fn(
        api_key,
        from_addr,
        to_addr,
        report_subject(report),
        report_email_html(report, portal_url),
        attachments=[pdf_attachment(report, pdf_bytes)],
    )


__all__ = [
    "pdf_attachment",
    "report_email_html",
    "report_subject",
    "send_report_email",
]
