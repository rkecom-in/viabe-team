"""VT-86 — monthly impact report rendering (HTML assembly + PDF).

Two layers, split so the bulk is locally testable without system libs:
  - `render_report_html(report)` — PURE: builds a self-contained HTML document
    (inline CSS, inline SVG bars — no matplotlib per D7) from a MonthlyReport,
    in the tenant's language (EN/HI). Fully unit-testable.
  - `render_report_pdf(report)` — thin weasyprint wrapper (HTML -> PDF bytes).
    weasyprint needs cairo/pango system libs (D1, in the orchestrator
    Dockerfile); tests importorskip it and the canary verifies real bytes.

Pillar 7 (honesty): a zero-ARRR or low-engagement month renders an explicit
"what happened / why" panel — never glossed. Pillar 8: this is a separate
generator from Reports' pdf_generator.py (different product/brand).
"""

from __future__ import annotations

from orchestrator.owner_surface.monthly_report import MonthlyReport

# Section + copy labels per language. Latin numerals are acceptable in Hindi
# (row note); Devanagari script for the words.
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Impact Report",
        "hero_label": "Attributed revenue this month",
        "campaigns_sent": "Campaigns sent",
        "customers_reached": "New customers",
        "approvals": "Approvals",
        "top_campaigns": "Top campaigns by revenue",
        "decision_split": "Your approval decisions",
        "approved": "Approved",
        "rejected": "Rejected",
        "pending": "Pending",
        "footer_residency": "Data stored in India. Processed per the DPDP Act.",
        "contact": "Questions? Reply on WhatsApp.",
        "trial_note": "You're in trial — here's what happened so far.",
        "zero_arrr": "This month had no attributed revenue. Likely reasons: "
                     "no campaigns sent, low attribution, or an ingestion gap.",
        "low_engagement": "Few campaigns were approved this month — approving "
                          "more is the main lever on revenue.",
    },
    "hi": {
        "title": "प्रभाव रिपोर्ट",
        "hero_label": "इस महीने जिम्मेदार राजस्व",
        "campaigns_sent": "भेजे गए अभियान",
        "customers_reached": "नए ग्राहक",
        "approvals": "स्वीकृतियाँ",
        "top_campaigns": "राजस्व के अनुसार शीर्ष अभियान",
        "decision_split": "आपके स्वीकृति निर्णय",
        "approved": "स्वीकृत",
        "rejected": "अस्वीकृत",
        "pending": "लंबित",
        "footer_residency": "डेटा भारत में संग्रहीत। DPDP अधिनियम के अनुसार संसाधित।",
        "contact": "प्रश्न? WhatsApp पर उत्तर दें।",
        "trial_note": "आप ट्रायल में हैं — अब तक यह हुआ।",
        "zero_arrr": "इस महीने कोई जिम्मेदार राजस्व नहीं रहा। संभावित कारण: "
                     "कोई अभियान नहीं भेजा गया, कम एट्रिब्यूशन, या इनजेशन गैप।",
        "low_engagement": "इस महीने कम अभियान स्वीकृत हुए — अधिक स्वीकृति "
                          "राजस्व का मुख्य कारक है।",
    },
}

_HERO_POSITIVE = "#1a7f37"  # green
_HERO_ZERO = "#6b7280"      # gray (never red — Pillar 7 framing, not blame)


def _lang(report: MonthlyReport) -> dict[str, str]:
    return LABELS.get(report.language, LABELS["en"])


def money_inr(paise: int) -> str:
    """Format integer paise as ₹ rupees (no paise fraction for headline use)."""
    rupees = paise // 100
    return f"₹{rupees:,}"


def _bar_svg(approved: int, rejected: int, pending: int) -> str:
    """Inline SVG horizontal stacked bar for the approval split (D7 — no
    matplotlib). Degrades to a single 'no decisions yet' bar at zero total."""
    total = approved + rejected + pending
    if total == 0:
        return ('<svg width="100%" height="22" role="img">'
                '<rect width="100%" height="22" fill="#e5e7eb"/></svg>')
    width = 480
    seg = []
    x = 0
    for count, color in ((approved, "#1a7f37"), (rejected, "#9ca3af"),
                         (pending, "#d1d5db")):
        w = round(width * count / total)
        if w > 0:
            seg.append(f'<rect x="{x}" y="0" width="{w}" height="22" fill="{color}"/>')
            x += w
    return f'<svg width="{width}" height="22" role="img">{"".join(seg)}</svg>'


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_report_html(report: MonthlyReport) -> str:
    """Build the self-contained HTML document for the report. Pure + testable.

    No customer PII is rendered — only the business name, aggregate counts, and
    campaign IDs (CL-390). The hero is green when ARRR>0, gray at zero (never
    red — honest, not punitive)."""
    L = _lang(report)
    hero_color = _HERO_POSITIVE if report.arrr_paise > 0 else _HERO_ZERO

    honesty_blocks = []
    if report.trial_framing:
        honesty_blocks.append(f'<p class="note trial">{L["trial_note"]}</p>')
    if report.zero_arrr:
        honesty_blocks.append(f'<p class="note">{L["zero_arrr"]}</p>')
    if report.low_engagement and not report.zero_arrr:
        honesty_blocks.append(f'<p class="note">{L["low_engagement"]}</p>')
    honesty_html = "".join(honesty_blocks)

    top_rows = "".join(
        f"<tr><td>{_esc(c.campaign_id[:8])}</td><td>{money_inr(c.arrr_paise)}</td></tr>"
        for c in report.top_campaigns
    ) or '<tr><td colspan="2">—</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="{report.language}"><head><meta charset="utf-8">
<style>
  body {{ font-family: sans-serif; color: #111827; margin: 0; padding: 32px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .period {{ color: #6b7280; font-size: 13px; }}
  .hero {{ font-size: 40px; font-weight: 700; color: {hero_color}; margin: 24px 0 4px; }}
  .hero-label {{ color: #6b7280; font-size: 13px; }}
  .cards {{ display: flex; gap: 16px; margin: 24px 0; }}
  .card {{ flex: 1; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; }}
  .card .n {{ font-size: 24px; font-weight: 600; }}
  .card .l {{ color: #6b7280; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0 24px; }}
  td, th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #f3f4f6; font-size: 13px; }}
  .note {{ background: #f9fafb; border-left: 3px solid #9ca3af; padding: 10px 12px;
           font-size: 13px; margin: 8px 0; }}
  .note.trial {{ border-left-color: #1a7f37; }}
  footer {{ color: #9ca3af; font-size: 11px; margin-top: 32px; border-top: 1px solid #f3f4f6; padding-top: 12px; }}
</style></head><body>
  <h1>{_esc(report.business_name)} — {L["title"]}</h1>
  <div class="period">{report.year_month}</div>

  <div class="hero">{money_inr(report.arrr_paise)}</div>
  <div class="hero-label">{L["hero_label"]}</div>

  <div class="cards">
    <div class="card"><div class="n">{report.campaigns_sent}</div><div class="l">{L["campaigns_sent"]}</div></div>
    <div class="card"><div class="n">{report.customers_added}</div><div class="l">{L["customers_reached"]}</div></div>
    <div class="card"><div class="n">{report.approved_count}</div><div class="l">{L["approvals"]}</div></div>
  </div>

  {honesty_html}

  <h3>{L["decision_split"]}</h3>
  {_bar_svg(report.approved_count, report.rejected_count, report.pending_count)}
  <div class="period">{L["approved"]}: {report.approved_count} &nbsp; {L["rejected"]}: {report.rejected_count} &nbsp; {L["pending"]}: {report.pending_count}</div>

  <h3>{L["top_campaigns"]}</h3>
  <table><tbody>{top_rows}</tbody></table>

  <footer>{L["footer_residency"]}<br>{L["contact"]}</footer>
</body></html>"""


def render_report_pdf(report: MonthlyReport) -> bytes:
    """Render the report HTML to PDF bytes via weasyprint.

    weasyprint requires cairo/pango system libs (provisioned in the
    orchestrator Dockerfile, D1). Imported lazily so the pure HTML path and the
    rest of the module load on dev machines without those libs."""
    from weasyprint import HTML  # lazy: system-dep, not importable everywhere

    html = render_report_html(report)
    return HTML(string=html).write_pdf()


__all__ = ["LABELS", "money_inr", "render_report_html", "render_report_pdf"]
