#!/usr/bin/env python3
"""build_sprint_dashboard.py — sprint-level view (one card per sprint).

The PM dashboard (`viabe-team-pm-dashboard`) shows every VT row. This one
shows sprint-level progress only: status, Done/Total, progress bar, critical
path summary, recent milestones. Designed for a "where are we" glance, not a
"what's the next row" decision.

Usage:
    python scripts/build_sprint_dashboard.py [output.html]
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPRINT = REPO / ".viabe" / "sprint"
CLAU = REPO / "docs" / "clau"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Sprint canonical order + status verdict (manual, sprint-level).
# Each tuple: (sprint_label, sprint_status, short_name, blurb_line)
SPRINTS = [
    ("Pre-Sprint 0 - Pillars & Setup", "Completed", "Pre-Sprint 0",
     "Pillars + concept-team baseline."),
    ("Sprint 1 - Foundation", "Completed", "Sprint 1 — Foundation",
     "Repo + DBOS + LangGraph + RLS + Logfire + observability + brain-wiring. Critical-path closed."),
    ("Sprint 1.5 - Hardening", "Active (passive)", "Sprint 1.5 — Hardening",
     "Backlog of follow-ups from Sprint 1 (DBOS purge retrofits, evaluator wiring)."),
    ("Sprint 2 - Integration Agent (re-anchored)", "In Progress", "Sprint 2 — Integration Agent",
     "Onboarding + recurring ingestion across customer data sources. Substrate complete; 2 manual walks pending."),
    ("Sprint 2 - Cost + Moat", "Partial / Deferred", "Sprint 2 — Cost+Moat",
     "Prompt caching landed; rest deferred per CL-420. Ops console + alerts shipped opportunistically."),
    ("Sprint 2 - SR Agent Skeleton", "Pre-Sprint-2 work", "Sprint 2 — SR-Agent",
     "Anthropic Agent SDK skeleton + 11 MCP tools. Most tools queued as backlog rows."),
    ("Sprint 2 - Owner Surface", "Future", "Sprint 2 — Owner Surface",
     "VT-189 Ops Console v2 backlog parked."),
    ("Sprint 3 - Ingestion Methods 1-2", "Future", "Sprint 3 — Ingest 1-2",
     "Paper book photograph + phone contacts list import."),
    ("Sprint 4 - Ingestion Methods 3-5", "Future", "Sprint 4 — Ingest 3-5",
     "UPI export + KOT/POS + cashbook+voice."),
    ("Sprint 5 - Online Methods 6-9", "Future", "Sprint 5 — Online 6-9",
     "QR opt-in + Apify Zomato/Swiggy/GBP + WhatsApp NL entry."),
    ("Sprint 6 - Tools Batch 2", "Future", "Sprint 6 — Tools Batch 2",
     "(Empty — placeholder.)"),
    ("Sprint 7 - Knowledge Architecture", "Future", "Sprint 7 — KG",
     "4-layer KG / episodic / Layer-3 / skills. Privacy architecture."),
    ("Sprint 8 - Owner Surface & Billing", "Future", "Sprint 8 — Owner Surf + Billing",
     "Razorpay Live + landing page + sign-up + founding-tier counter. Reports-Jun15 launch cluster."),
    ("Sprint 9 - Polish & E2E", "Future", "Sprint 9 — Polish & E2E",
     "Tech Reference, ADRs, runbooks, source-of-truth banners."),
    ("Hardening", "Open queue", "Hardening (loose)",
     "Soak + Meta templates + Razorpay vendor approvals. Open queue not strictly part of any sprint."),
    ("Vendor Approvals Buffer", "Open queue", "Vendor Approvals",
     "Razorpay Live + Apify + Twilio DLT + KYC + Resend + LangSmith billing + DPDPA final review."),
]

STATUS_STYLES = {
    "Completed":          ("#14532d", "#d3f0d6", "DONE"),
    "Active (passive)":   ("#2b3a55", "#e3e8ef", "PASSIVE"),
    "In Progress":        ("#9b1c1c", "#fde8e7", "ACTIVE"),
    "Partial / Deferred": ("#74570e", "#fdf3d3", "PARTIAL"),
    "Pre-Sprint-2 work":  ("#74570e", "#fdf3d3", "INTERLEAVED"),
    "Future":             ("#5b6470", "#eaecef", "FUTURE"),
    "Open queue":         ("#5b6470", "#f1f2f4", "QUEUE"),
}


def parse_frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^(\w[\w_]*)\s*:\s*(.*)$", line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip('"').strip("'")
    return out


def load_sprint_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for f in sorted(SPRINT.glob("VT-*.md")):
        fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        if not fm:
            continue
        fm.setdefault("status", "")
        fm.setdefault("priority", "")
        fm.setdefault("sprint", "")
        fm.setdefault("title", f.stem)
        rows.append(fm)
    return rows


def git_log_recent(n: int = 10) -> list[tuple[str, str]]:
    try:
        out = subprocess.check_output(
            ["git", "log", "--oneline", f"-{n}"],
            cwd=REPO, stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    pairs = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def days_to(target_iso: str) -> int:
    return (dt.date.fromisoformat(target_iso) - dt.datetime.now(IST).date()).days


def now_ist_str() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_sprint_card(label: str, status: str, short: str, blurb: str,
                       rows: list[dict[str, str]]) -> str:
    counts = Counter(r.get("status", "") for r in rows)
    n_total = len(rows)
    n_done = counts.get("Done", 0)
    n_active = sum(counts.get(s, 0) for s in ("Queued", "To Do", "In Progress", "Review"))
    n_backlog = counts.get("Backlog", 0)
    n_deferred = counts.get("Deferred", 0) + counts.get("Cancelled", 0)

    pct = round(100 * n_done / n_total) if n_total else 0

    # Active + critical-not-done summary
    crit_not_done = [r for r in rows
                     if r.get("priority") == "Critical" and r.get("status") != "Done"]
    in_progress = [r for r in rows if r.get("status") == "In Progress"]

    fg, bg, badge_txt = STATUS_STYLES.get(status, ("#5b6470", "#eaecef", status.upper()))

    # Bar color matches status
    bar_color = {
        "Completed": "#2da450",
        "Active (passive)": "#5e7fb4",
        "In Progress": "#e87514",
        "Partial / Deferred": "#c79518",
        "Pre-Sprint-2 work": "#c79518",
        "Future": "#a7adb5",
        "Open queue": "#a7adb5",
    }.get(status, "#a7adb5")

    # In-flight rows list
    in_progress_html = ""
    if in_progress:
        items = "".join(
            f'<li><b>{esc(r.get("vt_id",""))}</b> {esc(r.get("title","")[:80])}</li>'
            for r in in_progress[:6]
        )
        in_progress_html = f'<div class="ip"><div class="ip-h">In flight</div><ul>{items}</ul></div>'

    crit_html = ""
    if status not in ("Completed", "Future") and crit_not_done:
        items = "".join(
            f'<li><b>{esc(r.get("vt_id",""))}</b> {esc(r.get("title","")[:80])} '
            f'<span class="muted">[{esc(r.get("status","") or "—")}]</span></li>'
            for r in crit_not_done[:5]
        )
        crit_html = f'<div class="cb"><div class="cb-h">Critical not-Done ({len(crit_not_done)})</div><ul>{items}</ul></div>'

    breakdown = (
        f'<span class="b-done">{n_done} Done</span>'
        + (f' · <span class="b-active">{n_active} Active</span>' if n_active else '')
        + (f' · <span class="b-back">{n_backlog} Backlog</span>' if n_backlog else '')
        + (f' · <span class="b-def">{n_deferred} Def/Cnl</span>' if n_deferred else '')
    )

    return f"""
<div class="sprint-card">
  <div class="sc-head">
    <div>
      <div class="sc-title">{esc(short)}</div>
      <div class="sc-blurb">{esc(blurb)}</div>
    </div>
    <div class="sc-badge" style="color:{fg};background:{bg};">{esc(badge_txt)}</div>
  </div>
  <div class="bar-wrap">
    <div class="bar"><div class="bar-fill" style="width:{pct}%;background:{bar_color};"></div></div>
    <div class="bar-meta"><b>{n_done}/{n_total}</b> · {pct}% Done</div>
  </div>
  <div class="breakdown">{breakdown}</div>
  {in_progress_html}
  {crit_html}
</div>
"""


def build_html(rows: list[dict[str, str]]) -> str:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        groups[r.get("sprint", "").strip()].append(r)

    n_total = len(rows)
    n_done = sum(1 for r in rows if r.get("status") == "Done")
    days_left = days_to("2026-06-15")
    recent = git_log_recent(10)

    # Total milestone progress
    overall_pct = round(100 * n_done / n_total) if n_total else 0

    # Recent merges block
    recent_html = "".join(
        f'<div class="merge"><code>{esc(sha)}</code> {esc(msg)}</div>'
        for sha, msg in recent
    )

    # Group cards by status bucket
    status_buckets: dict[str, list[str]] = {
        "Active now":           [],
        "Completed":            [],
        "Open queues / passive": [],
        "Future sprints":       [],
    }
    bucket_map = {
        "Completed": "Completed",
        "Active (passive)": "Open queues / passive",
        "In Progress": "Active now",
        "Partial / Deferred": "Active now",
        "Pre-Sprint-2 work": "Active now",
        "Future": "Future sprints",
        "Open queue": "Open queues / passive",
    }

    unknown_sprints = [s for s in groups.keys() if s and not any(s == lbl for lbl, _, _, _ in SPRINTS)]
    sprint_specs = list(SPRINTS) + [(s, "Open queue", s, "(Auto-detected sprint not in canonical list.)") for s in sorted(unknown_sprints)]

    for label, status, short, blurb in sprint_specs:
        cards = groups.get(label, [])
        if not cards:
            continue
        html = render_sprint_card(label, status, short, blurb, cards)
        status_buckets[bucket_map.get(status, "Future sprints")].append(html)

    sections_html = []
    for bucket_name, bucket_cards in status_buckets.items():
        if not bucket_cards:
            continue
        sections_html.append(
            f'<h2>{esc(bucket_name)} <span class="muted">· {len(bucket_cards)} sprint(s)</span></h2>'
            f'<div class="grid">{"".join(bucket_cards)}</div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<title>Viabe Team — Sprint Dashboard</title>
<style>
:root {{ color-scheme: light only !important; }}
@media (prefers-color-scheme: dark) {{
  :root {{ color-scheme: light only !important; }}
  html, body {{ background: #fafbfc !important; color: #1b1f24 !important; }}
}}
html, body {{
  margin: 0; padding: 0;
  background: #fafbfc !important;
  color: #1b1f24 !important;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif;
  font-size: 14px; line-height: 1.45;
}}
.wrap {{ max-width: 1280px; margin: 0 auto; padding: 22px 28px 56px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; font-weight: 700; }}
h2 {{ font-size: 14px; margin: 28px 0 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #2b3a55; padding-bottom: 4px; border-bottom: 1px solid #e6e8eb; }}
.meta {{ color: #5b6470; font-size: 12px; margin-bottom: 18px; }}
.meta b {{ color: #1b1f24; }}
.topline {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 12px; padding: 16px 18px; margin: 14px 0 8px; display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 12px; }}
.tl-cell {{ }}
.tl-n {{ font-size: 24px; font-weight: 700; line-height: 1.1; }}
.tl-l {{ font-size: 11px; color: #5b6470; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }}
.tl-pri {{ display: flex; align-items: center; gap: 10px; }}
.tl-pri .barg {{ flex: 1; background: #eaecef; border-radius: 8px; height: 10px; overflow: hidden; }}
.tl-pri .bargf {{ height: 100%; background: linear-gradient(90deg,#2da450 0%,#5fb978 100%); border-radius: 8px; }}
.muted {{ color: #5b6470; font-weight: 500; font-size: 12px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }}
.sprint-card {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 14px 16px; }}
.sc-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }}
.sc-title {{ font-weight: 700; font-size: 14px; color: #1b1f24; }}
.sc-blurb {{ font-size: 12px; color: #5b6470; margin-top: 2px; line-height: 1.4; }}
.sc-badge {{ font-size: 10px; font-weight: 700; padding: 4px 8px; border-radius: 6px; letter-spacing: 0.05em; white-space: nowrap; }}
.bar-wrap {{ display: flex; align-items: center; gap: 10px; margin: 10px 0 6px; }}
.bar {{ flex: 1; background: #eaecef; border-radius: 6px; height: 8px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 6px; transition: width 0.3s ease; }}
.bar-meta {{ font-size: 11px; color: #5b6470; font-weight: 600; white-space: nowrap; }}
.bar-meta b {{ color: #1b1f24; }}
.breakdown {{ font-size: 11px; color: #5b6470; margin-bottom: 8px; }}
.b-done {{ color: #14532d; font-weight: 600; }}
.b-active {{ color: #9b1c1c; font-weight: 600; }}
.b-back {{ color: #5b6470; }}
.b-def {{ color: #5b6470; font-style: italic; }}
.ip, .cb {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #eaecef; font-size: 12px; }}
.ip-h, .cb-h {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #5b6470; margin-bottom: 4px; }}
.ip ul, .cb ul {{ margin: 0; padding-left: 16px; }}
.ip li, .cb li {{ margin-bottom: 2px; line-height: 1.4; }}
.cb-h {{ color: #9b1c1c; }}
.merge {{ background: #f0f8f1; border-left: 3px solid #2da450; padding: 6px 10px; border-radius: 0 4px 4px 0; margin-bottom: 4px; font-size: 12px; }}
.merge code {{ background: transparent; font-weight: 600; color: #14532d; }}
.recent {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 14px 16px; margin-top: 14px; }}
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #eaecef; color: #5b6470; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">

<h1>Viabe Team — Sprint Dashboard</h1>
<div class="meta">
  Generated <b>{now_ist_str()}</b> · Source: <code>.viabe/sprint/VT-*.md</code> + sprint status (manual) ·
  Reports-Jun15 in <b>{days_left} days</b><br>
  <b style="color:#14532d;">Sprint-level view</b> · For per-row detail open <code>viabe-team-pm-dashboard</code>.
</div>

<div class="topline">
  <div class="tl-cell tl-pri">
    <div>
      <div class="tl-n">{overall_pct}%</div>
      <div class="tl-l">Overall {n_done}/{n_total} rows Done</div>
    </div>
    <div class="barg"><div class="bargf" style="width:{overall_pct}%;"></div></div>
  </div>
  <div class="tl-cell">
    <div class="tl-n">2</div>
    <div class="tl-l">Sprints active now</div>
  </div>
  <div class="tl-cell">
    <div class="tl-n">3</div>
    <div class="tl-l">Sprints completed</div>
  </div>
  <div class="tl-cell">
    <div class="tl-n">{days_left}</div>
    <div class="tl-l">Days to Reports-Jun15</div>
  </div>
</div>

{"".join(sections_html)}

<div class="recent">
  <h2 style="margin-top:0; border:none; padding:0;">Recent merges on main</h2>
  {recent_html or '<i class=muted>No git log available.</i>'}
</div>

<div class="footer">
Sources: <code>.viabe/sprint/VT-*.md</code> ({n_total} rows), <code>git log --oneline</code>.<br>
Sprint status verdicts are manual (encoded in <code>scripts/build_sprint_dashboard.py SPRINTS</code>).
Light-mode lock enforced (Fazal directive).
</div>

</div>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    out_path = Path(argv[0]) if argv else None
    rows = load_sprint_rows()
    html = build_html(rows)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"wrote {len(html)} bytes to {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
