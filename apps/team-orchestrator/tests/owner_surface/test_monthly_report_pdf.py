"""VT-86 — monthly report rendering tests.

HTML assembly is pure → fully tested locally. The weasyprint PDF step needs
cairo/pango system libs (D1, Dockerfile) → importorskip-gated; the canary
verifies real PDF bytes in an env that has the libs.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.owner_surface.monthly_report import (  # noqa: E402
    MonthlyReport,
    TopCampaign,
)
from orchestrator.owner_surface.monthly_report_pdf import (  # noqa: E402
    money_inr,
    render_report_html,
    render_report_pdf,
)


def _report(**over):
    base = dict(
        tenant_id=str(uuid4()), year_month="2026-04", business_name="Sharma Cafe",
        language="en", trial_framing=False,
        campaign_status_counts={"proposed": 1, "approved": 3, "rejected": 1,
                                "sent": 4, "failed": 0},
        approved_count=3, rejected_count=1, pending_count=1,
        arrr_paise=4_250_00, top_campaigns=[TopCampaign(campaign_id="abcdef12-3456", arrr_paise=3_000_00)],
        customers_added=7, customers_added_prior_month=4,
    )
    base.update(over)
    return MonthlyReport(**base)


def test_money_inr_formats_paise_to_rupees():
    assert money_inr(4_250_00) == "₹4,250"   # 425000 paise → ₹4,250
    assert money_inr(0) == "₹0"
    assert money_inr(50_00) == "₹50"          # 5000 paise → ₹50


def test_html_has_core_sections_en():
    html = render_report_html(_report())
    assert "Sharma Cafe" in html
    assert "Impact Report" in html
    assert "2026-04" in html
    assert "₹4,250" in html              # hero ARRR
    assert "Campaigns sent" in html
    assert "Top campaigns" in html
    assert "DPDP" in html                # footer residency disclosure


def test_hero_green_when_positive_gray_when_zero():
    assert "#1a7f37" in render_report_html(_report(arrr_paise=50000))
    zero = render_report_html(_report(arrr_paise=0))
    assert "#6b7280" in zero
    assert "#1a7f37" not in zero.split('class="hero"')[1][:60]  # hero itself gray


def test_zero_arrr_renders_honest_copy():
    html = render_report_html(_report(arrr_paise=0, approved_count=0, pending_count=0,
                                      top_campaigns=[]))
    assert "no attributed revenue" in html.lower()


def test_low_engagement_copy_when_few_approvals():
    html = render_report_html(_report(approved_count=1, arrr_paise=10000))
    assert "approving" in html.lower() or "main lever" in html.lower()


def test_trial_framing_note():
    html = render_report_html(_report(trial_framing=True))
    assert "trial" in html.lower()


def test_hindi_labels_render():
    html = render_report_html(_report(language="hi"))
    assert "प्रभाव रिपोर्ट" in html           # Hindi title
    assert "Impact Report" not in html        # not the EN title
    assert "Sharma Cafe" in html              # business name unchanged


def test_no_customer_pii_in_html():
    """Only business_name + aggregate counts + truncated campaign IDs render —
    no customer names/phones (CL-390)."""
    html = render_report_html(_report())
    # campaign id is truncated to 8 chars, not the full UUID.
    assert "abcdef12" in html
    assert "abcdef12-3456" not in html


def test_bar_svg_handles_zero_total():
    html = render_report_html(_report(approved_count=0, rejected_count=0, pending_count=0))
    assert "<svg" in html  # degrades to an empty gray bar, not a crash


def test_render_pdf_produces_pdf_bytes():
    """weasyprint render — skipped where cairo/pango aren't installed (dev
    macOS); the canary verifies this in an env with the libs."""
    pytest.importorskip("weasyprint")
    pdf = render_report_pdf(_report())
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"
