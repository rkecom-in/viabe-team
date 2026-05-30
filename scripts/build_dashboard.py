#!/usr/bin/env python3
"""build_dashboard.py — regenerate the Viabe Team PM dashboard from repo files.

Reads `.viabe/sprint/VT-*.md` (167 files), `docs/clau/decisions-ledger.md`,
`docs/clau/latest-snapshot.md`, and `git log` to produce a self-contained HTML
dashboard. Writes to a path you pass on the CLI (default: stdout).

Usage:
    python scripts/build_dashboard.py [output.html]

Designed to be invoked by a scheduled task that then calls `update_artifact`
on the Cowork artifact `viabe-team-pm-dashboard`. Light-mode lock is hard-coded
(Fazal directive: dashboard MUST always be light).
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

# Canonical sprint order — used for grouping + filter dropdown ordering.
SPRINT_ORDER = [
    "Pre-Sprint 0 - Pillars & Setup",
    "Sprint 1 - Foundation",
    "Sprint 1.5 - Hardening",
    "Sprint 2 - Integration Agent (re-anchored)",
    "Sprint 2 - Cost + Moat",
    "Sprint 2 - SR Agent Skeleton",
    "Sprint 2 - Owner Surface",
    "Sprint 3 - Ingestion Methods 1-2",
    "Sprint 4 - Ingestion Methods 3-5",
    "Sprint 5 - Online Methods 6-9",
    "Sprint 6 - Tools Batch 2",
    "Sprint 7 - Knowledge Architecture",
    "Sprint 8 - Owner Surface & Billing",
    "Sprint 9 - Polish & E2E",
    "Hardening",
    "Vendor Approvals Buffer",
]
SPRINT_SHORT = {
    "Pre-Sprint 0 - Pillars & Setup": "Pre-0 Pillars",
    "Sprint 1 - Foundation": "S1 Foundation",
    "Sprint 1.5 - Hardening": "S1.5 Hardening",
    "Sprint 2 - Integration Agent (re-anchored)": "S2 IntegAgent",
    "Sprint 2 - Cost + Moat": "S2 Cost+Moat",
    "Sprint 2 - SR Agent Skeleton": "S2 SR-Agent",
    "Sprint 2 - Owner Surface": "S2 OwnerSurf",
    "Sprint 3 - Ingestion Methods 1-2": "S3 Ingest 1-2",
    "Sprint 4 - Ingestion Methods 3-5": "S4 Ingest 3-5",
    "Sprint 5 - Online Methods 6-9": "S5 Online 6-9",
    "Sprint 6 - Tools Batch 2": "S6 Tools-2",
    "Sprint 7 - Knowledge Architecture": "S7 Knowledge",
    "Sprint 8 - Owner Surface & Billing": "S8 Owner+Bill",
    "Sprint 9 - Polish & E2E": "S9 Polish",
    "Hardening": "Hardening",
    "Vendor Approvals Buffer": "Vendor Bf",
}


def parse_frontmatter(text: str) -> dict[str, str]:
    """Tolerant YAML-ish parser. Returns a flat str→str dict.

    Notion-migrated files have mixed quoting / colons in titles / multi-line
    values. PyYAML chokes on some; we do line-by-line `key: value` extraction
    for the keys we care about.
    """
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^(\w[\w_]*)\s*:\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            val = kv.group(2).strip().strip('"').strip("'")
            out[key] = val
    return out


def load_sprint_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for f in sorted(SPRINT.glob("VT-*.md")):
        fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        if not fm:
            continue
        fm["_filename"] = f.name
        fm.setdefault("status", "")
        fm.setdefault("priority", "")
        fm.setdefault("parent", "")
        fm.setdefault("sprint", "")
        fm.setdefault("title", f.stem)
        rows.append(fm)
    return rows


def load_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def git_log_recent(n: int = 6) -> list[tuple[str, str]]:
    """Return [(sha, message)] for the last n commits on current HEAD."""
    try:
        out = subprocess.check_output(
            ["git", "log", "--oneline", f"-{n}"],
            cwd=REPO,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    pairs = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def status_pill_class(status: str) -> str:
    return {
        "Done": "s-done",
        "Queued": "s-queued",
        "To Do": "s-todo",
        "In Progress": "s-todo",
        "Review": "s-review",
        "Blocked": "s-blocked",
        "Deferred": "s-deferred",
        "Backlog": "s-backlog",
    }.get(status, "s-backlog")


def priority_pill_class(priority: str) -> str:
    return {
        "Critical": "p-crit",
        "High": "p-high",
        "Medium": "p-med",
        "Low": "p-low",
    }.get(priority, "p-low")


def now_ist_str() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def days_to(target_iso: str) -> int:
    target = dt.date.fromisoformat(target_iso)
    today = dt.datetime.now(IST).date()
    return (target - today).days


def build_html(rows: list[dict[str, str]]) -> str:
    n_total = len(rows)
    by_status = Counter(r.get("status", "") for r in rows)
    n_done = by_status.get("Done", 0)
    n_backlog = by_status.get("Backlog", 0)
    n_deferred = by_status.get("Deferred", 0)
    n_active = sum(by_status.get(s, 0) for s in ("Queued", "To Do", "In Progress", "Review"))
    n_blocked = by_status.get("Blocked", 0)

    crit_not_done = [
        r for r in rows if r.get("priority") == "Critical" and r.get("status") != "Done"
    ]
    n_crit_not_done = len(crit_not_done)

    # By parent
    parents: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        p = r.get("parent", "").strip()
        if p:
            parents[p].append(r)

    parent_rows = [r for r in rows if not r.get("parent", "").strip()]

    # Active queue (Queued / To Do / In Progress / Review), sorted by priority
    pri_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "": 4}
    active = [r for r in rows if r.get("status") in ("Queued", "To Do", "In Progress", "Review")]
    active.sort(key=lambda r: (pri_rank.get(r.get("priority", ""), 4), r.get("vt_id", "")))

    # Sprint groupings — for the by-sprint section + filter dropdown.
    sprint_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        sp = r.get("sprint", "").strip() or "(no sprint)"
        sprint_groups[sp].append(r)

    # Sprint × status matrix for the rollup table.
    sprint_status_counts: dict[str, Counter] = {}
    for sp, items in sprint_groups.items():
        sprint_status_counts[sp] = Counter(r.get("status", "") for r in items)

    # Ordered sprint list — canonical order first, then any unknowns.
    sprints_seen = list(sprint_groups.keys())
    sprints_ordered = [s for s in SPRINT_ORDER if s in sprints_seen]
    sprints_ordered += [s for s in sprints_seen if s not in SPRINT_ORDER]

    # Critical Backlog (often hidden under summary)
    crit_backlog = [r for r in rows if r.get("priority") == "Critical" and r.get("status") == "Backlog"]
    crit_backlog.sort(key=lambda r: r.get("vt_id", ""))

    # Git recent
    recent = git_log_recent(6)

    # Latest snapshot
    snapshot = load_text(CLAU / "latest-snapshot.md")

    # Ledger count
    ledger_text = load_text(CLAU / "decisions-ledger.md")
    n_ledger = len([l for l in ledger_text.splitlines() if l.startswith("- **CL-")])

    # Session log entry count (file count)
    n_cl_entries = len(list((CLAU / "entries").glob("CL-*.md"))) if (CLAU / "entries").exists() else 0

    # Days to Jun15
    days_left = days_to("2026-06-15")

    # Build per-parent stats table
    parent_stat_rows = []
    parent_rows_sorted = sorted(parent_rows, key=lambda r: int(r.get("vt_id", "VT-0").split("-")[1]))
    for p in parent_rows_sorted:
        vt_id = p.get("vt_id", "")
        children = parents.get(vt_id, [])
        total = len(children)
        if total == 0:
            parent_stat_rows.append(
                f'<tr><td class="vt">{vt_id} {esc(p.get("title", "")[:48])}</td>'
                f'<td colspan="5"><i class="gap">0 children (inline-only or empty parent)</i></td></tr>'
            )
            continue
        cdone = sum(1 for c in children if c.get("status") == "Done")
        cback = sum(1 for c in children if c.get("status") == "Backlog")
        cactive = sum(1 for c in children if c.get("status") in ("Queued", "To Do", "In Progress", "Review"))
        cdef = sum(1 for c in children if c.get("status") == "Deferred")
        cblk = sum(1 for c in children if c.get("status") == "Blocked")
        title_short = p.get("title", "")[:48]
        parent_stat_rows.append(
            f'<tr><td class="vt">{vt_id} {esc(title_short)}</td>'
            f'<td>{total}</td>'
            f'<td>{cdone}</td>'
            f'<td>{cactive}</td>'
            f'<td>{cback}</td>'
            f'<td>{cdef + cblk}</td></tr>'
        )

    # Active queue table — tagged with data-sprint for filter
    active_rows_html = []
    for r in active[:30]:
        pri = r.get("priority", "—")
        sp = r.get("sprint", "(no sprint)") or "(no sprint)"
        active_rows_html.append(
            f'<tr data-sprint="{esc(sp)}"><td class="vt">{r.get("vt_id", "")}</td>'
            f'<td><span class="pill {priority_pill_class(pri)}">{esc(pri)}</span></td>'
            f'<td><span class="pill {status_pill_class(r.get("status", ""))}">{esc(r.get("status", ""))}</span></td>'
            f'<td>{esc(r.get("title", "")[:90])}</td>'
            f'<td>{esc(r.get("parent", ""))}</td></tr>'
        )

    # Critical backlog table — tagged with data-sprint
    cb_rows_html = []
    for r in crit_backlog[:25]:
        sp = r.get("sprint", "(no sprint)") or "(no sprint)"
        cb_rows_html.append(
            f'<tr data-sprint="{esc(sp)}"><td class="vt">{r.get("vt_id", "")}</td>'
            f'<td>{esc(r.get("title", "")[:90])}</td>'
            f'<td>{esc(r.get("parent", ""))}</td></tr>'
        )

    # Sprint × Status rollup matrix HTML
    statuses_order = ["Done", "Queued", "To Do", "In Progress", "Review", "Backlog", "Blocked", "Deferred"]
    sprint_matrix_header = "<tr><th>Sprint</th>" + "".join(f"<th>{s}</th>" for s in statuses_order) + "<th>Total</th></tr>"
    sprint_matrix_rows = []
    for sp in sprints_ordered:
        counts = sprint_status_counts.get(sp, Counter())
        total = sum(counts.values())
        cells = "".join(f"<td>{counts.get(s, 0) or '·'}</td>" for s in statuses_order)
        short = SPRINT_SHORT.get(sp, sp)
        sprint_matrix_rows.append(
            f'<tr data-sprint="{esc(sp)}"><td class="vt" title="{esc(sp)}">{esc(short)}</td>{cells}<td><b>{total}</b></td></tr>'
        )
    sprint_matrix_html = sprint_matrix_header + "".join(sprint_matrix_rows)

    # Per-sprint task sections — one section per sprint, all rows in it
    sprint_section_html = []
    pri_rank_local = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "": 4}
    for sp in sprints_ordered:
        items = sorted(
            sprint_groups.get(sp, []),
            key=lambda r: (pri_rank_local.get(r.get("priority", ""), 4), r.get("status", "ZZ"), r.get("vt_id", "")),
        )
        if not items:
            continue
        counts = sprint_status_counts.get(sp, Counter())
        done_n = counts.get("Done", 0)
        total_n = sum(counts.values())
        rows_html = []
        for r in items:
            pri = r.get("priority", "—") or "—"
            st = r.get("status", "") or "—"
            rows_html.append(
                f'<tr><td class="vt">{r.get("vt_id", "")}</td>'
                f'<td><span class="pill {priority_pill_class(pri)}">{esc(pri)}</span></td>'
                f'<td><span class="pill {status_pill_class(st)}">{esc(st)}</span></td>'
                f'<td>{esc(r.get("title", "")[:90])}</td>'
                f'<td>{esc(r.get("parent", ""))}</td></tr>'
            )
        sprint_section_html.append(
            f'<div class="sprint-section panel" data-sprint="{esc(sp)}">'
            f'<h3>{esc(sp)} — {done_n}/{total_n} Done</h3>'
            f'<table><thead><tr><th>VT</th><th>Pri</th><th>Status</th><th>Title</th><th>Parent</th></tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody></table>'
            f'</div>'
        )

    # Sprint filter dropdown options
    sprint_filter_options = '<option value="ALL">All sprints</option>'
    for sp in sprints_ordered:
        n_items = len(sprint_groups.get(sp, []))
        sprint_filter_options += f'<option value="{esc(sp)}">{esc(SPRINT_SHORT.get(sp, sp))} ({n_items})</option>'

    # Recent merges
    recent_html = []
    for sha, msg in recent:
        recent_html.append(f'<div class="merge-pr"><div class="ttl"><code>{sha}</code> — {esc(msg)}</div></div>')

    # Snapshot (preserve markdown roughly)
    snap_html = (
        snapshot.replace("## ", "<h4>").replace("\n\n", "</h4><p>") + "</p>"
        if snapshot else "<p><i>Snapshot file missing.</i></p>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>Viabe Team — PM Dashboard</title>
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
h2 {{ font-size: 16px; margin: 26px 0 10px; font-weight: 600; }}
h3 {{ font-size: 13px; margin: 14px 0 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: #5b6470; }}
h4 {{ font-size: 12px; margin: 12px 0 4px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #5b6470; }}
.meta {{ color: #5b6470; font-size: 12px; margin-bottom: 18px; }}
.meta b {{ color: #1b1f24; }}
.stats {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; margin: 14px 0 8px; }}
.stat {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 12px 14px; }}
.stat .n {{ font-size: 22px; font-weight: 700; line-height: 1.1; }}
.stat .l {{ font-size: 11px; color: #5b6470; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }}
.panel {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 16px 18px; margin-top: 14px; }}
.row2 {{ display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: left; font-size: 11px; text-transform: uppercase; color: #5b6470; letter-spacing: 0.04em; padding: 8px 8px 6px; border-bottom: 1px solid #eaecef; }}
td {{ padding: 7px 8px; border-bottom: 1px solid #f1f2f4; vertical-align: top; }}
tr:last-child td {{ border-bottom: none; }}
.vt {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 600; white-space: nowrap; }}
.pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; line-height: 1.55; white-space: nowrap; }}
.p-crit {{ background: #fde8e7; color: #9b1c1c; }}
.p-high {{ background: #fdebd0; color: #8a4a14; }}
.p-med {{ background: #fdf3d3; color: #74570e; }}
.p-low {{ background: #e8eef5; color: #2c3e50; }}
.s-done {{ background: #d3f0d6; color: #14532d; }}
.s-queued {{ background: #e3e8ef; color: #2b3a55; }}
.s-todo {{ background: #fde8e7; color: #9b1c1c; }}
.s-review {{ background: #fdf3d3; color: #74570e; }}
.s-blocked {{ background: #f5d4ef; color: #5b1c5b; }}
.s-deferred {{ background: #eaecef; color: #5b6470; }}
.s-backlog {{ background: #f1f2f4; color: #5b6470; }}
.merge-pr {{ background: #f0f8f1; border-left: 3px solid #2da450; padding: 8px 12px; border-radius: 0 6px 6px 0; margin-bottom: 6px; }}
.merge-pr .ttl {{ font-weight: 600; font-size: 12px; }}
code {{ background: #f4f5f7; padding: 1px 5px; border-radius: 3px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
.small {{ font-size: 12px; color: #5b6470; }}
.gap {{ color: #5b6470; font-style: italic; }}
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #eaecef; color: #5b6470; font-size: 12px; }}
.snap {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 14px 18px; font-size: 13px; line-height: 1.5; }}
.snap p {{ margin: 4px 0 10px; }}
.filter-bar {{ position: sticky; top: 0; background: #fafbfc; padding: 10px 0 8px; border-bottom: 1px solid #e6e8eb; margin: 14px 0 4px; z-index: 10; display: flex; align-items: center; gap: 12px; }}
.filter-bar label {{ font-size: 12px; font-weight: 600; color: #5b6470; text-transform: uppercase; letter-spacing: 0.04em; }}
.filter-bar select {{ font-size: 13px; padding: 5px 10px; border: 1px solid #d4d8de; border-radius: 6px; background: #fff; color: #1b1f24; min-width: 220px; }}
.filter-bar .clear {{ font-size: 11px; color: #2b3a55; text-decoration: underline; cursor: pointer; background: none; border: none; padding: 0; }}
.sprint-section {{ margin-top: 10px; }}
.sprint-section h3 {{ margin-top: 0; color: #2b3a55; text-transform: none; letter-spacing: 0; font-size: 14px; }}
.matrix td, .matrix th {{ text-align: center; }}
.matrix td:first-child, .matrix th:first-child {{ text-align: left; }}
.matrix tbody tr:hover {{ background: #f8f9fb; }}
</style>
</head>
<body>
<div class="wrap">

<h1>Viabe Team — PM Dashboard</h1>
<div class="meta">
  Generated <b>{now_ist_str()}</b> · Source: <code>.viabe/sprint/</code> (live) · Reports-Jun15 in <b>{days_left} days</b><br>
  <b style="color:#14532d;">Source of truth: repo files</b> · Notion frozen as read-only archive · Auto-regen every 10 min
</div>

<div class="stats">
  <div class="stat"><div class="n">{n_total}</div><div class="l">Total VT rows</div></div>
  <div class="stat"><div class="n">{n_done}</div><div class="l">Done</div></div>
  <div class="stat"><div class="n">{n_active}</div><div class="l">Active queue</div></div>
  <div class="stat"><div class="n">{n_backlog}</div><div class="l">Backlog</div></div>
  <div class="stat"><div class="n">{n_crit_not_done}</div><div class="l">Critical not-Done</div></div>
  <div class="stat"><div class="n">{n_ledger}</div><div class="l">Standing decisions</div></div>
  <div class="stat"><div class="n">{n_cl_entries}</div><div class="l">CL entries</div></div>
</div>

<h2>Latest snapshot (5-field)</h2>
<div class="snap">{snap_html}</div>

<h2>Recent commits on main</h2>
{"".join(recent_html) or "<p class=small><i>No git log available</i></p>"}

<div class="filter-bar">
  <label for="sprintFilter">Filter by sprint:</label>
  <select id="sprintFilter" onchange="applySprintFilter()">{sprint_filter_options}</select>
  <button class="clear" onclick="document.getElementById('sprintFilter').value='ALL'; applySprintFilter();">clear</button>
  <span class="small" id="filterStatus" style="margin-left:auto;"></span>
</div>

<h2>Sprint × Status rollup</h2>
<div class="panel">
<table class="matrix">
<thead>{sprint_matrix_html.split('</tr>', 1)[0]}</tr></thead>
<tbody>{sprint_matrix_html.split('</tr>', 1)[1] if '</tr>' in sprint_matrix_html else ''}</tbody>
</table>
</div>

<h2>By sprint — {len(sprints_ordered)} sprints, click filter above to focus</h2>
{"".join(sprint_section_html) or "<p class=small><i>No sprint sections</i></p>"}

<h2>Active queue — {len(active)} rows by priority (cross-sprint)</h2>
<div class="panel">
<table>
<thead><tr><th>VT</th><th>Pri</th><th>Status</th><th>Title</th><th>Parent</th></tr></thead>
<tbody>
{"".join(active_rows_html) or "<tr><td colspan=5><i class=gap>No active rows</i></td></tr>"}
</tbody>
</table>
</div>

<h2>Critical Backlog — {len(crit_backlog)} rows not yet queued (cross-sprint)</h2>
<div class="panel">
<table>
<thead><tr><th>VT</th><th>Title</th><th>Parent</th></tr></thead>
<tbody>
{"".join(cb_rows_html) or "<tr><td colspan=3><i class=gap>No Critical Backlog</i></td></tr>"}
</tbody>
</table>
</div>

<h2>Parent inventory ({len(parent_rows_sorted)} parents)</h2>
<div class="panel">
<table>
<thead><tr><th>Parent</th><th>Total</th><th>Done</th><th>Active</th><th>Backlog</th><th>Def/Blk</th></tr></thead>
<tbody>
{"".join(parent_stat_rows)}
</tbody>
</table>
</div>

<div class="footer">
Sources: <code>.viabe/sprint/VT-*.md</code> (167), <code>docs/clau/latest-snapshot.md</code>, <code>docs/clau/decisions-ledger.md</code> ({n_ledger} Standing), <code>docs/clau/entries/CL-*.md</code> ({n_cl_entries} entries), <code>git log</code>.<br>
Regenerated by <code>scripts/build_dashboard.py</code>. Light-mode lock enforced (Fazal directive).
</div>

<script>
function applySprintFilter() {{
  const want = document.getElementById('sprintFilter').value;
  const statusEl = document.getElementById('filterStatus');
  // Per-sprint sections — hide all except the selected one
  document.querySelectorAll('.sprint-section').forEach(sec => {{
    if (want === 'ALL' || sec.dataset.sprint === want) {{
      sec.style.display = '';
    }} else {{
      sec.style.display = 'none';
    }}
  }});
  // Tagged rows in other tables (active queue, critical backlog, sprint matrix)
  let visibleRows = 0;
  document.querySelectorAll('tr[data-sprint]').forEach(tr => {{
    if (want === 'ALL' || tr.dataset.sprint === want) {{
      tr.style.display = '';
      visibleRows++;
    }} else {{
      tr.style.display = 'none';
    }}
  }});
  if (want === 'ALL') {{
    statusEl.textContent = '';
  }} else {{
    const sel = document.getElementById('sprintFilter');
    statusEl.textContent = `filtering: ${{sel.options[sel.selectedIndex].text}} — ${{visibleRows}} rows visible`;
  }}
  // Persist choice across regens
  try {{ localStorage.setItem('viabe-pm-sprint-filter', want); }} catch(e) {{}}
}}

// Restore prior filter on load
try {{
  const saved = localStorage.getItem('viabe-pm-sprint-filter');
  if (saved && document.getElementById('sprintFilter')) {{
    const sel = document.getElementById('sprintFilter');
    for (let i = 0; i < sel.options.length; i++) {{
      if (sel.options[i].value === saved) {{
        sel.selectedIndex = i;
        applySprintFilter();
        break;
      }}
    }}
  }}
}} catch(e) {{}}
</script>

</div>
</body>
</html>
"""


def esc(s: str) -> str:
    """Minimal HTML escape — enough for titles + parent strings."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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
