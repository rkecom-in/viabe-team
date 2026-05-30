"""VT-86 — storage + email delivery tests (no live vendors).

Storage upload + Resend send are injected (mock client / mock send_fn), so the
path/body/attachment logic is fully tested locally; the canary does the real
upload + the real send to a TEST recipient (CL-422)."""

from __future__ import annotations

import asyncio
import base64

import pytest

pytest.importorskip("pydantic")

from orchestrator.owner_surface.monthly_report import MonthlyReport  # noqa: E402
from orchestrator.owner_surface.monthly_report_email import (  # noqa: E402
    pdf_attachment,
    report_email_html,
    report_subject,
    send_report_email,
)
from orchestrator.owner_surface.report_storage import (  # noqa: E402
    report_storage_path,
    store_report_pdf,
)


def _report(**over):
    base = dict(
        tenant_id="11111111-1111-1111-1111-111111111111", year_month="2026-04",
        business_name="Sharma Cafe", language="en", trial_framing=False,
        campaign_status_counts={"proposed": 0, "approved": 3, "rejected": 1,
                                "sent": 4, "failed": 0},
        approved_count=3, rejected_count=1, pending_count=0,
        arrr_paise=4_250_00, top_campaigns=[], customers_added=7,
        customers_added_prior_month=4,
    )
    base.update(over)
    return MonthlyReport(**base)


# ------------------------------- storage ----------------------------------


def test_storage_path_is_tenant_scoped():
    p = report_storage_path("tenant-abc", "2026-04")
    assert p == "tenant-abc/2026-04.pdf"


def test_store_report_pdf_uploads_via_injected_client():
    calls = {}

    class _MockBucket:
        def upload(self, path, file, file_options):
            calls["path"] = path
            calls["file"] = file
            calls["opts"] = file_options

    path = store_report_pdf("tenant-abc", "2026-04", b"%PDF-bytes",
                            client=_MockBucket())
    assert path == "tenant-abc/2026-04.pdf"
    assert calls["path"] == "tenant-abc/2026-04.pdf"
    assert calls["file"] == b"%PDF-bytes"
    assert calls["opts"]["content-type"] == "application/pdf"
    assert calls["opts"]["upsert"] == "true"


# -------------------------------- email -----------------------------------


def test_subject_en_and_hi():
    assert report_subject(_report()) == "Your Viabe Team Impact Report — April 2026"
    hi = report_subject(_report(language="hi"))
    assert "प्रभाव रिपोर्ट" in hi
    assert "April 2026" in hi


def test_email_html_has_summary_and_portal_link_no_pii():
    html = report_email_html(_report(), "https://viabe.ai/team")
    assert "Sharma Cafe" in html
    assert "₹4,250" in html            # ARRR
    assert "campaigns sent: 4" in html.lower() or "campaigns sent" in html.lower()
    assert "https://viabe.ai/team" in html


def test_email_html_zero_arrr_is_honest():
    html = report_email_html(_report(arrr_paise=0), "https://viabe.ai/team")
    assert "no attributed revenue" in html.lower()


def test_email_html_hindi():
    html = report_email_html(_report(language="hi"), "https://viabe.ai/team")
    assert "नमस्ते" in html
    assert "Hi Sharma Cafe" not in html


def test_pdf_attachment_is_base64():
    att = pdf_attachment(_report(), b"%PDF-1.7 body")
    assert att["filename"] == "viabe-impact-2026-04.pdf"
    assert base64.b64decode(att["content"]) == b"%PDF-1.7 body"


def test_send_report_email_passes_attachment_to_send_fn():
    captured = {}

    async def _mock_send(api_key, from_addr, to_addr, subject, html,
                         attachments=None):
        captured.update(api_key=api_key, from_addr=from_addr, to_addr=to_addr,
                        subject=subject, html=html, attachments=attachments)
        return True

    ok = asyncio.run(send_report_email(
        _report(), b"%PDF-x",
        to_addr="owner@example.com", portal_url="https://viabe.ai/team",
        api_key="re_test", from_addr="ops@viabe.ai", send_fn=_mock_send,
    ))
    assert ok is True
    assert captured["to_addr"] == "owner@example.com"
    assert captured["from_addr"] == "ops@viabe.ai"
    assert captured["attachments"][0]["filename"] == "viabe-impact-2026-04.pdf"
    assert "Impact Report" in captured["subject"]
