# Viabe Team — bootstrap for any Claude session

You (Claude) just got pointed at this repo. **Read these three files first, in this order, before answering any question or starting any work.** They are short and they contain everything you need to operate.

1. **`docs/clau/operating-brief.md`** — defines the four-role model (Fazal/Cowork/Claude Code/Clau), the sequencing principle, and how decisions flow. ~5 min read. Don't skip — most fresh sessions misroute decisions without it.
2. **`docs/clau/latest-snapshot.md`** — the 5-field State Snapshot: Critical Path / In Flight / Blocked On / Next Action / Do Not. This is the current briefing. If a question is about "what are we working on" or "what's next," the answer is in here.
3. **`docs/clau/decisions-ledger.md`** — flat list of every Standing decision with originating CL number. Don't re-litigate anything in here.

After those three, you have full project context. The remaining files below are reference, fetched when needed.

---

## What this project is

**Viabe Team** — a multi-agent system for small Indian business owners. WhatsApp-first, owner-facing portal at viabe.ai/team. Three deployable apps in a Python+Next.js monorepo:
- `apps/team-orchestrator/` (Python 3.13, DBOS + LangGraph + Anthropic SDK, the critical path)
- `apps/team-web/` (Next.js 16, React 19, webhooks + marketing + dashboard + ops UI)
- `apps/team-ingestion-worker/` (Python 3.13, Apify + Sarvam, currently a SystemExit stub)
- `packages/team-shared/` (cross-app types)

**Binding launch milestone:** Reports-Jun15 (2026-06-15). Sprints 1+2 ship for that gate; everything else is ship-thin.

**Repo:** `github.com/rkecom-in/viabe-team` (public). Local clone at `/Users/fazalkhan/development/viabe-team`.

---

## The four roles (operating model, full text in `docs/clau/operating-brief.md`)

| Role | Owns |
|---|---|
| **Fazal (CEO)** | All final calls. Product, pricing, privacy/legal, scope, launch. Can override anything. |
| **Cowork (delivery captain)** | The tracker, sprint progress, status reconciliation, daily briefs, rostering rows, routing work to Claude Code. Decides within-sprint operational matters using the standing sequencing rules. Runs the loop **without Clau** by default. |
| **Claude Code (implementer)** | Decision role inside a task — implementation approach, code-level design, refactors, library use, tests, bug fixes. MUST log every material step + decision so Clau's audit layer has substrate. |
| **Clau (architect)** | Implementation strategy + cross-sprint sequencing. Audit-AFTER, not approval-before. Runs at sprint boundaries, on request, or when something looks off. |

---

## Source of truth (cutover 2026-05-25)

| What | Where | NOT here |
|---|---|---|
| Task board / sprint rows | `.viabe/sprint/VT-<N>.md` (167 files) | Notion ViabeTeam_Sprint (read-only archive) |
| Session log entries | `docs/clau/entries/CL-<N>.md` (369 files) | Notion Clau_Session_Log (read-only archive) |
| Standing decisions | `docs/clau/decisions-ledger.md` | — |
| Latest snapshot | `docs/clau/latest-snapshot.md` | — |
| Discipline rules | `docs/clau/discipline-rules.md` | — |
| Operating brief | `docs/clau/operating-brief.md` | — |
| Resurrection file | `docs/clau/resurrection-file.md` *(pending Clau dump)* | — |

If you ever find yourself about to query Notion for VT row state, **stop** — read the local `.viabe/sprint/VT-<N>.md` file instead. Notion is frozen.

---

## How to find a thing

| You want | Run |
|---|---|
| One VT row by ID | `cat .viabe/sprint/VT-<N>.md` |
| All Critical-priority active rows | `grep -l "priority: Critical" .viabe/sprint/VT-*.md` then check `status:` |
| Session log entries by topic | `grep -l "<topic>" docs/clau/entries/CL-*.md` |
| A Standing decision | `grep -i "<keyword>" docs/clau/decisions-ledger.md` |
| Recent merges | `git log --oneline -10` |
| The next VT-ID for a new row | `python scripts/vt_id_allocate.py --peek` (consume: drop `--peek`) |
| Current dashboard | open the Cowork artifact `viabe-team-pm-dashboard` (Cowork sessions only) |

---

## Standing disciplines (full text in `docs/clau/discipline-rules.md`)

- **Rule #14:** every status summary, sprint order, or handoff is reconciled against ground truth (`gh pr list --state merged` + the log files) before trusted. Memory is never authoritative. Applies to Clau's summaries too.
- **Rule #15:** every brief touching external API / SDK / persistence MUST include a canary acceptance step. Real API call, verify response, fail-not-skip on error. Cowork bounces plan-ready signals without canary plans.
- **Rule #16:** before Cowork dispatches any `brief-ready` signal, Cowork MUST run `python3 scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md` and add a `cl_decisions_checked: [CL-N, ...]` frontmatter field to the signal listing every active-context row the script surfaced. Claude Code bounces brief-ready signals missing that field. Triggered by Cowork's 2026-05-25 LangSmith drift (Cowork shipped VT-101/102/103/104 against CL-56 Standing without reading it). Substrate: `docs/clau/active-context-summary.md` — Cowork-maintained digest, updated on every important decision / change / merge / Fazal directive.
- **Pillar 7 (merges):** every PR merge requires Fazal's explicit authorization. Never auto-merge. The mechanism is a `type: task` signal with `authorized_by: fazal` in `.running/to-claudecode/`.
- **VT-IDs are numeric-only.** Never invent text-suffix IDs like `VT-FOO`. Allocator at `scripts/vt_id_allocate.py` claims monotonic numeric IDs under flock.
- **Don't re-litigate Standing decisions.** If it's in the ledger, it's settled.
- **Before asking Fazal anything:** state what you checked (ledger + snapshot). Bare questions without that line get bounced.
- **Dashboard is light-mode only** — hard CSS lock in the artifact.

---

## Pipeline architecture

- **Inbox/outbox:** `.running/to-claudecode/` (Cowork → CC) + `.running/to-cowork/` (CC → Cowork) + `.running/processed/` (archive).
- **Protocol:** `.viabe/protocol.md` — signal types, frontmatter schemas, escalation rules.
- **Orchestrators:** interactive `claude -c` watch loop (Fazal's primary today); Python daemon at `.viabe/daemon/` (installed but paused via `.viabe/daemon/STOP` file).
- **Cowork poller:** scheduled task `viabe-team-queue-poller`, every 3 min 24/7.
- **Dashboard regen:** scheduled task `viabe-team-dashboard-regen`, every 10 min 24/7.

---

## What's notably NOT here

- **`docs/clau/resurrection-file.md`** is empty / missing — Clau owes a dump. Not blocking but it's the deep-context file for fresh Clau sessions.
- **Discipline rules #6, #7, #10, #11** are TODO in `discipline-rules.md`. The migration extracted 10 of 14 from session log entries.
- **A new State Snapshot post-2026-05-25 migration** — `latest-snapshot.md` still anchors at CL-407 (2026-05-24 session close) and predates the overnight migration, audit work, and 4 PRs landed. Compress when next session ends.

---

## How NOT to behave

- **Don't re-derive what the snapshot already says.** If `latest-snapshot.md` says the critical path is X, that's the answer.
- **Don't trust your own memory across sessions** — the auto-memory at `~/Library/Application Support/Claude/.../spaces/<id>/memory/` is **per-space**. A new Cowork window, a Dispatch thread, or a phone session does NOT see it. The repo files are the only cross-space substrate.
- **Don't roster a new VT row without using the allocator** (`scripts/vt_id_allocate.py`). The Notion `auto_increment_id` is gone; the file counter at `.viabe/sprint/.next-id` is the replacement.
- **Don't write to Notion.** It's a read-only archive. Every Cowork/CC/Clau write goes to the `.viabe/sprint/` or `docs/clau/` files.

---

## If something is unclear

Per Rule #14: check the ledger + snapshot first, then ask. Don't ask without stating what you checked.

Cross-refs that are deeper than this file:
- Brief audit history: `docs/clau/entries/CL-322.md`, `CL-386.md`, `CL-389.md`, `CL-390.md`
- Migration story: `docs/clau/operating-brief.md` §3
- Sprint board schema: `.viabe/sprint/README.md`
- Pipeline protocol: `.viabe/protocol.md`

---

*Authored 2026-05-25 ~13:00 IST by Cowork (delivery captain) after Fazal flagged that a fresh Dispatch session had zero project context. This file ensures any future Claude session with read access to this repo can self-bootstrap.*
