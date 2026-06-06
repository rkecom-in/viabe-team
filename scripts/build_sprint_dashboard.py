#!/usr/bin/env python3
"""build_sprint_dashboard.py — the SPRINT dashboard (VT-355: layout frozen, data live).

Emits EXACTLY the Fazal-approved layout (`.viabe/dashboard-approved-template.html`): the same
structure / CSS / JS, with ONLY data interpolated from `.viabe/sprint/VT-*.md` + git. Two runs
on unchanged data are byte-identical (deterministic ordering everywhere). The mock banner is
removed; light-mode is hard-locked. The sprint-brief paragraph is inlined verbatim from the
Cowork-maintained `.viabe/sprint-brief.md` (NOT generated prose).

Do NOT touch the PM-dashboard script — this is the SPRINT dashboard only.

3.10-COMPATIBLE: the `viabe-team-dashboard-regen` scheduled task runs on Python 3.10 — keep this
script 3.10-clean. In particular, NEVER put a backslash inside an f-string expression (a 3.12+
feature) — hoist such pieces to a variable/helper (see `_gate_tag`).

Usage:
    python scripts/build_sprint_dashboard.py [output.html]
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPRINT = REPO / ".viabe" / "sprint"
BRIEF_FILE = REPO / ".viabe" / "sprint-brief.md"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MILESTONE = "2026-06-15"
MILESTONE_NAME = "Reports-Jun15"

# status (frontmatter) -> (filter class, status dot colour). Matches the approved template.
STATUS_MAP: dict[str, tuple[str, str]] = {
    "Done": ("f-done", "#2da450"),
    "In Progress": ("f-flight", "#2b6cb0"),
    "Queued": ("f-flight", "#0d8a8a"),
    "Blocked": ("f-flight", "#c4451c"),
    "Backlog": ("f-back", "#9aa3ad"),
    "Deferred": ("f-back", "#9aa3ad"),
    "Cancelled": ("f-closed", "#b7a6e8"),
}
_CLOSED = {"Done", "Cancelled"}  # "closed" = Done + Cancelled (the overall-% numerator basis)
_FLIGHT = {"In Progress", "Queued", "Blocked"}
_BACK = {"Backlog", "Deferred"}

# Deterministic sprint-group order (the approved template's section order). Groups not listed
# fall to the end, sorted by name (kept stable so a new sprint never reorders the rest).
SPRINT_ORDER = [
    "Pre-Sprint 0 - Pillars & Setup",
    "Sprint 1 - Foundation",
    "Sprint 1.5 - Hardening",
    "Sprint 2 - Cost + Moat",
    "Sprint 2 - SR Agent Skeleton",
    "Sprint 2 - Owner Surface",
    "Sprint 2 - Integration Agent (re-anchored)",
    "Sprint 3 - Ingestion Methods 1-2",
    "Sprint 3 - IntegrationInvention",
    "Sprint 4 - Ingestion Methods 3-5",
    "Sprint 5 - Online Methods 6-9",
    "Sprint 7 - Knowledge Architecture",
    "Sprint 8 - Owner Surface & Billing",
    "Sprint 8 - Billing & Signup",
    "Sprint 9 - Launch Surface",
    "Sprint 9 - Polish & E2E",
    "Hardening",
    "Sprint-Fazal",
    "Backlog - Agent Operation Layer",
    "Backlog - Ingestion Ease",
]

# Phase-status table: (display, [member sprint names], badge-class, badge-text, curated note).
# closed/total are DERIVED from the member rows; the note + badge are curated (Cowork-owned).
PHASE_GROUPS: list[tuple[str, list[str], str, str, str]] = [
    ("Sprints 0 – 5 (Foundation → Online Methods)",
     ["Pre-Sprint 0 - Pillars & Setup", "Sprint 1 - Foundation", "Sprint 1.5 - Hardening",
      "Sprint 2 - Cost + Moat", "Sprint 2 - SR Agent Skeleton", "Sprint 2 - Owner Surface",
      "Sprint 2 - Integration Agent (re-anchored)", "Sprint 3 - Ingestion Methods 1-2",
      "Sprint 3 - IntegrationInvention", "Sprint 4 - Ingestion Methods 3-5",
      "Sprint 5 - Online Methods 6-9"],
     "bg-done", "COMPLETE", "deferred/backlog stragglers only"),
    ("Sprint 7 — Knowledge Architecture", ["Sprint 7 - Knowledge Architecture"],
     "bg-done", "COMPLETE", "blocked/backlog non-critical"),
    ("Sprint 8 — Owner Surface &amp; Billing", ["Sprint 8 - Owner Surface & Billing"],
     "bg-active", "ACTIVE", "critical path done; queue + gates remain"),
    ("Sprint 8 — Billing &amp; Signup", ["Sprint 8 - Billing & Signup"],
     "bg-active", "ACTIVE", "VT-326 done; flip is Fazal-gated"),
    ("Sprint 9 — Launch Surface + Polish &amp; E2E",
     ["Sprint 9 - Launch Surface", "Sprint 9 - Polish & E2E"],
     "bg-active", "ACTIVE", "legal pages VT-353"),
    ("Hardening", ["Hardening"], "bg-hold", "ROLLING", "queued + backlog"),
    ("Sprint-Fazal", ["Sprint-Fazal"], "bg-fazal", "NEEDS FAZAL", "your queue — creds, copy, rulings"),
    ("Backlogs (Agent-Op Layer · Ingestion Ease)",
     ["Backlog - Agent Operation Layer", "Backlog - Ingestion Ease"],
     "bg-hold", "PARKED", "post-launch"),
]

_BADGE_BAR = {"bg-done": "g", "bg-active": "b", "bg-hold": "gr", "bg-fazal": "gr"}

CSS = """:root { color-scheme: light only !important; }
@media (prefers-color-scheme: dark) { html, body { background:#f7f8fa !important; color:#1b1f24 !important; } }
html,body{margin:0;padding:0;background:#f7f8fa!important;color:#1b1f24!important;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;font-size:14px;line-height:1.45}
.wrap{max-width:1240px;margin:0 auto;padding:20px 26px 52px}
.mockbanner{background:#fff7e6;border:1px solid #f0d9a8;color:#7a5b13;border-radius:8px;padding:8px 14px;font-size:12px;font-weight:600;margin-bottom:14px}
h1{font-size:21px;margin:0;font-weight:700}
.meta{color:#5b6470;font-size:12px;margin:4px 0 16px}
.meta b{color:#1b1f24}
h2{font-size:13px;margin:26px 0 10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#2b3a55;border-bottom:1px solid #e4e7ea;padding-bottom:5px}
.top{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:6px}
.kpi{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:14px 16px}
.kpi .n{font-size:26px;font-weight:700;line-height:1.05}
.kpi .l{font-size:11px;color:#5b6470;text-transform:uppercase;letter-spacing:.05em;margin-top:5px}
.kpi.warn .n{color:#9b1c1c}
.kpi.ok .n{color:#14532d}
.brief{background:#fff;border:1px solid #e4e7ea;border-left:4px solid #2b6cb0;border-radius:10px;padding:13px 16px;font-size:13px;line-height:1.55;margin-top:12px}
.brief b{color:#1b1f24}
table.phases{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e4e7ea;border-radius:10px;overflow:hidden}
table.phases th{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#5b6470;text-align:left;padding:9px 12px;background:#fbfcfd;border-bottom:1px solid #e4e7ea}
table.phases td{padding:8px 12px;border-bottom:1px solid #f0f2f4;font-size:13px;vertical-align:middle}
table.phases tr:last-child td{border-bottom:none}
.ph-name{font-weight:600;white-space:nowrap}
.bar{background:#eaecef;border-radius:5px;height:8px;min-width:130px;overflow:hidden}
.bf{height:100%;border-radius:5px}
.bf.g{background:#2da450}.bf.b{background:#2b6cb0}.bf.gr{background:#9aa3ad}
.frac{font-size:12px;color:#5b6470;font-weight:600;white-space:nowrap}
.badge{font-size:10px;font-weight:700;padding:3px 8px;border-radius:5px;letter-spacing:.04em;white-space:nowrap}
.bg-done{background:#e7f5ea;color:#14532d}.bg-active{background:#e8f0fb;color:#1d4f91}.bg-hold{background:#f2f3f5;color:#5b6470}.bg-fazal{background:#fdeaea;color:#9b1c1c}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:13px 16px}
.card h3{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px;color:#5b6470}
.card h3.red{color:#9b1c1c}
.card ul{margin:0;padding-left:17px;font-size:13px}
.card li{margin-bottom:5px;line-height:1.45}
.card li code{background:#f2f3f5;padding:1px 5px;border-radius:4px;font-size:12px;font-weight:600}
.tag{font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;margin-left:5px}
.tag.crit{background:#fdeaea;color:#9b1c1c}.tag.gate{background:#fff3e0;color:#8a5a00}.tag.now{background:#e8f0fb;color:#1d4f91}
.merges{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:13px 16px}
.m-row{display:flex;gap:10px;padding:5px 0;border-bottom:1px dashed #f0f2f4;font-size:12.5px;align-items:baseline}
.m-row:last-child{border-bottom:none}
.m-row code{color:#14532d;font-weight:700;white-space:nowrap}
.m-row .pr{color:#5b6470;font-weight:600;white-space:nowrap}
.footer{margin-top:28px;padding-top:14px;border-top:1px solid #e4e7ea;color:#5b6470;font-size:11.5px}

/* sprint board */
.pills{display:flex;gap:8px;align-items:center;margin:4px 0 16px;flex-wrap:wrap}
.pill{border:1px solid #d6dade;background:#fff;color:#1b1f24;font-size:12.5px;font-weight:600;padding:7px 16px;border-radius:999px;cursor:pointer}
.pill.active{background:#1b1f24;color:#fff;border-color:#1b1f24}
.showing{margin-left:auto;font-size:12px;color:#5b6470}
.sblock{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:6px 16px 10px;margin-bottom:14px}
.shead{display:flex;justify-content:space-between;align-items:baseline;padding:10px 0 6px}
.sname{font-weight:700;font-size:15px}
.scount{font-size:12px;color:#5b6470}
table.srows{width:100%;border-collapse:collapse}
table.srows td{padding:7px 8px;border-top:1px solid #f0f2f4;font-size:13px;vertical-align:baseline}
.rid{font-weight:700;white-space:nowrap;width:70px}
.rst{white-space:nowrap;width:120px;color:#3a4350}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}
.rtitle{color:#1b1f24}
.row.hid{display:none}
.sblock.hid{display:none}

"""

JS = """(function(){
  var pills=document.querySelectorAll('.pill');
  function apply(f){
    var shown=0;
    document.querySelectorAll('tr.row').forEach(function(r){
      var ok=(f==='all')||r.classList.contains('f-'+f);
      r.classList.toggle('hid',!ok); if(ok) shown++;
    });
    document.querySelectorAll('.sblock').forEach(function(b){
      b.classList.toggle('hid', b.querySelectorAll('tr.row:not(.hid)').length===0);
    });
    document.getElementById('shn').textContent=shown;
  }
  pills.forEach(function(p){ p.addEventListener('click',function(){
    pills.forEach(function(q){q.classList.remove('active')}); p.classList.add('active'); apply(p.dataset.f);
  });});
})();"""

FOOTER = ("Light-mode locked · the single Viabe-Team dashboard — full per-row board with filters "
          "below · Regenerated by script; layout frozen to this approved template.")


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


def _vt_num(vt_id: str) -> int:
    m = re.search(r"(\d+)", vt_id or "")
    return int(m.group(1)) if m else 10**9


def load_sprint_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for f in sorted(SPRINT.glob("VT-*.md")):
        fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        if not fm:
            continue
        fm.setdefault("vt_id", f.stem)
        fm.setdefault("status", "")
        fm.setdefault("priority", "")
        fm.setdefault("sprint", "")
        fm.setdefault("title", f.stem)
        fm.setdefault("exec_order", "")
        fm.setdefault("tags", "")
        rows.append(fm)
    # Deterministic global order: by VT number.
    rows.sort(key=lambda r: _vt_num(r["vt_id"]))
    return rows


def git_log_recent(n: int = 30) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "log", "--oneline", "--no-color", f"-{n}", "main"],
            cwd=REPO, stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def days_to(target_iso: str) -> int:
    return (dt.date.fromisoformat(target_iso) - dt.datetime.now(IST).date()).days


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_gate(r: dict[str, str]) -> bool:
    tags = r.get("tags", "").lower()
    return ("launch-gate" in tags or "pre-live" in tags) and r["status"] not in _CLOSED


def _is_fazal(r: dict[str, str]) -> bool:
    return ("needs-fazal" in r.get("tags", "").lower() or r.get("sprint") == "Sprint-Fazal") \
        and r["status"] not in _CLOSED


def _gate_tag(r: dict[str, str]) -> str:
    # Hoisted out of the f-string (no backslash-in-expression — 3.10-compatible; see file note).
    return ('<span class="tag gate">GATE</span>' if _is_gate(r)
            else '<span class="tag crit">FAZAL</span>')


def build_html(rows: list[dict[str, str]], *, now: dt.datetime, brief: str,
               merges: list[tuple[str, str, str]]) -> str:
    total = len(rows)
    closed = sum(1 for r in rows if r["status"] in _CLOSED)
    pct = round(closed * 100 / total) if total else 0
    gates = sum(1 for r in rows if _is_gate(r))
    fazal = sum(1 for r in rows if _is_fazal(r))
    n_done = sum(1 for r in rows if r["status"] == "Done")
    n_flight = sum(1 for r in rows if r["status"] in _FLIGHT)
    n_back = sum(1 for r in rows if r["status"] in _BACK)
    n_cancel = sum(1 for r in rows if r["status"] == "Cancelled")

    meta = (f'Generated <b>{now.strftime("%Y-%m-%d %H:%M IST")}</b> · Source '
            f'<b>.viabe/sprint/VT-*.md</b> + git · Milestone <b>{MILESTONE_NAME}</b> in '
            f'<b style="color:#9b1c1c">{days_to(MILESTONE)} days</b>')

    kpis = (
        f'  <div class="kpi ok"><div class="n">{pct}%</div>'
        f'<div class="l">Overall · {closed} / {total} rows closed</div></div>\n'
        f'  <div class="kpi"><div class="n">{len(merges)}</div>'
        f'<div class="l">Recent merges shown</div></div>\n'
        f'  <div class="kpi warn"><div class="n">{gates}</div>'
        f'<div class="l">Launch gates open</div></div>\n'
        f'  <div class="kpi warn"><div class="n">{fazal}</div>'
        f'<div class="l">Blocked on Fazal</div></div>'
    )

    # Phase table (derived closed/total per group; curated badge + note).
    by_sprint: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_sprint.setdefault(r["sprint"], []).append(r)
    ph_rows = []
    for disp, members, badge_cls, badge_txt, note in PHASE_GROUPS:
        grp = [r for s in members for r in by_sprint.get(s, [])]
        g_total = len(grp)
        g_closed = sum(1 for r in grp if r["status"] in _CLOSED)
        g_pct = round(g_closed * 100 / g_total) if g_total else 0
        ph_rows.append(
            f'<tr><td class="ph-name">{disp}</td>'
            f'<td><div class="bar"><div class="bf {_BADGE_BAR[badge_cls]}" style="width:{g_pct}%"></div></div></td>'
            f'<td class="frac">{g_closed} / {g_total}</td>'
            f'<td><span class="badge {badge_cls}">{badge_txt}</span></td>'
            f'<td class="frac">{note}</td></tr>'
        )

    # Working-state two columns: left = non-done by exec_order (top 8); right = gates + fazal.
    def _exec(r: dict[str, str]) -> tuple[float, int]:
        try:
            return (float(r["exec_order"]), _vt_num(r["vt_id"]))
        except ValueError:
            return (1e9, _vt_num(r["vt_id"]))

    in_flight = sorted(
        [r for r in rows if r["status"] in _FLIGHT or (r["status"] in _BACK and r["exec_order"])],
        key=_exec,
    )[:8]
    left = "".join(
        f'      <li><code>{esc(r["vt_id"])}</code> {esc(r["title"])[:60]}</li>\n'
        for r in in_flight
    ) or "      <li>(nothing in flight)</li>\n"
    gate_rows = [r for r in rows if _is_gate(r) or _is_fazal(r)]
    right = "".join(
        f'      <li><code>{esc(r["vt_id"])}</code> {esc(r["title"])[:60]} {_gate_tag(r)}</li>\n'
        for r in gate_rows
    ) or "      <li>(no open gates)</li>\n"

    merges_html = "".join(
        f'  <div class="m-row"><code>{esc(pr)}</code><span class="pr">{esc(vt)}</span>'
        f'<span>{esc(subj)}</span></div>\n'
        for pr, vt, subj in merges
    )

    # Sprint board — grouped by sprint in SPRINT_ORDER, rows by VT number.
    order_index = {name: i for i, name in enumerate(SPRINT_ORDER)}
    group_keys = sorted(by_sprint.keys(), key=lambda s: (order_index.get(s, 10**6), s))
    blocks = []
    for s in group_keys:
        grp = sorted(by_sprint[s], key=lambda r: _vt_num(r["vt_id"]))
        gd = sum(1 for r in grp if r["status"] == "Done")
        gf = sum(1 for r in grp if r["status"] in _FLIGHT)
        gb = sum(1 for r in grp if r["status"] in _BACK)
        trs = []
        for r in grp:
            fclass, dot = STATUS_MAP.get(r["status"], ("f-back", "#9aa3ad"))
            trs.append(
                f'<tr class="row {fclass}"><td class="rid">{esc(r["vt_id"])}</td>'
                f'<td class="rst"><span class="dot" style="background:{dot}"></span>{esc(r["status"] or "—")}</td>'
                f'<td class="rtitle">{esc(r["title"])}</td></tr>'
            )
        blocks.append(
            f'<div class="sblock"><div class="shead"><span class="sname">{esc(s or "Unsorted")}</span>'
            f'<span class="scount">{gd}/{len(grp)} done · {gf} in-flight · {gb} backlog</span></div>'
            f'<table class="srows">\n' + "\n".join(trs) + "\n</table></div>"
        )
    board = "\n".join(blocks)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<title>Viabe Team — Sprint Dashboard</title>
<style>
{CSS}</style>
</head>
<body><div class="wrap">

<h1>Viabe Team — Sprint Dashboard</h1>
<div class="meta">{meta}</div>

<div class="top">
{kpis}
</div>

<div class="brief"><b>Sprint brief:</b> {brief}</div>

<h2>Phase status</h2>
<table class="phases">
<tr><th>Phase</th><th style="width:30%">Progress</th><th>Closed / Total</th><th>State</th><th>Note</th></tr>
{chr(10).join(ph_rows)}
</table>

<h2>Sprint 8 / 9 — working state</h2>
<div class="cols">
  <div class="card">
    <h3>In flight + next (CC queue, exec order)</h3>
    <ul>
{left}    </ul>
  </div>
  <div class="card">
    <h3 class="red">Gates — launch-blocking</h3>
    <ul>
{right}    </ul>
  </div>
</div>

<h2>Recent merges</h2>
<div class="merges">
{merges_html}</div>

<h2>Sprint board — all rows</h2>
<div class="pills">
<button class="pill active" data-f="all">All</button>
<button class="pill" data-f="done">Done ({n_done})</button>
<button class="pill" data-f="flight">In-flight ({n_flight})</button>
<button class="pill" data-f="back">Backlog ({n_back})</button>
<button class="pill" data-f="closed">Closed ({n_cancel})</button>
<span class="showing">Showing <b id="shn">{total}</b> of {total}</span></div>
{board}

<div class="footer">{FOOTER}</div>

</div>
<script>
{JS}
</script>

</body>
</html>
"""


_MERGE_RE = re.compile(r"^(?P<sha>\w+)\s+(?P<subj>.*?)(?:\s*\((?P<vt>VT-[\d.]+)[^)]*\))?(?:\s*\(#(?P<pr>\d+)\))?$")


def recent_merges(n: int = 8) -> list[tuple[str, str, str]]:
    """Last n main commits → (PR#, VT-id, subject). PR/VT parsed from the conventional
    '... (VT-N) (#PR)' suffix; missing parts degrade gracefully."""
    out: list[tuple[str, str, str]] = []
    for line in git_log_recent(n + 6):
        m = _MERGE_RE.match(line)
        if not m:
            continue
        subj = re.sub(r"\s*\(#\d+\)\s*$", "", m.group("subj") or "").strip()
        # Pull a VT id out of the subject if not in the trailing parens.
        vt = m.group("vt") or ""
        if not vt:
            mv = re.search(r"\bVT-[\d.]+", subj)
            vt = mv.group(0) if mv else ""
        pr = f'#{m.group("pr")}' if m.group("pr") else m.group("sha")[:7]
        out.append((pr, vt, subj))
        if len(out) >= n:
            break
    return out


def main() -> int:
    rows = load_sprint_rows()
    brief = "NEEDS-FAZAL — create .viabe/sprint-brief.md"
    if BRIEF_FILE.exists():
        brief = BRIEF_FILE.read_text(encoding="utf-8").strip()
    html = build_html(rows, now=dt.datetime.now(IST), brief=brief, merges=recent_merges(8))
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (REPO / "sprint_dashboard.html")
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
