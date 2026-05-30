# Viabe Team — bootstrap for any Claude session

You (Claude) just got pointed at this repo. **Read these files first, in this order, before answering any question or starting any work.** Skipping any of them is the #1 reason fresh sessions misroute work.

1. **`docs/clau/operating-brief.md`** — defines the four-role model (Fazal/Cowork/Claude Code/Clau), the sequencing principle, and how decisions flow. ~5 min read.
2. **`docs/clau/latest-snapshot.md`** — 5-field State Snapshot: Critical Path / In Flight / Blocked On / Next Action / Do Not. **Treat as suspect until reconciled.** This file drifts; see Rule #14 below.
3. **`docs/clau/decisions-ledger.md`** — flat list of every Standing decision with originating CL number. Do not re-litigate anything in here.
4. **`docs/clau/active-context-summary.md`** — Cowork-maintained digest of every active CL + brief contract. Required reading before any brief-ready dispatch (Rule #16).
5. **`docs/clau/discipline-rules.md`** — full text of Rules #1–17. Reference; read on demand.

After reading, **reconcile the snapshot against reality** before trusting it:

```bash
cd /Users/fazalkhan/development/viabe-team
git log --oneline -10
ls -lat .running/to-cowork/ .running/to-claudecode/
gh pr list --state open --limit 5
```

If the snapshot's HEAD / IN FLIGHT / NEXT ACTION doesn't match what `git log` shows, **regenerate the snapshot before doing anything else**. State the drift to Fazal — don't silently patch.

---

## What this project is

**Viabe Team** — a multi-agent system for small Indian business owners. WhatsApp-first, owner-facing portal at viabe.ai/team. Three deployable apps in a Python+Next.js monorepo:

- `apps/team-orchestrator/` (Python 3.13, DBOS + LangGraph + Anthropic SDK) — critical path
- `apps/team-web/` (Next.js 16, React 19) — webhooks + marketing + dashboard + Ops Console
- `apps/team-ingestion-worker/` (Python 3.13, Apify + Sarvam) — currently a SystemExit stub
- `packages/team-shared/` (cross-app types)

**Binding launch milestone:** Reports-Jun15 (2026-06-15). Sprints 1+2 ship for that gate; everything else is ship-thin.

**Repo:** `github.com/rkecom-in/viabe-team` (**private** — auth required for fetch/clone). Local clone at `/Users/fazalkhan/development/viabe-team`. **Main protection = an account-level ruleset; "Require status checks to pass" was turned OFF 2026-05-30 (VT-245)** — CI checks no longer block merges. The **local pre-push hook** (`scripts/git-hooks/pre-push`, install via `scripts/install-hooks.sh`) is the safety gate; CI is a non-blocking backstop on PRs. Route-via-PR remains a convention, not an enforced gate.

---

## The four roles (full text in `docs/clau/operating-brief.md`)

| Role | Owns |
|---|---|
| **Fazal (CEO)** | All final calls. Product, pricing, privacy/legal, scope, launch. Authorizes every merge (Pillar 7). Can override anything. |
| **Cowork (delivery captain)** | The tracker, sprint progress, status reconciliation, daily briefs, rostering, routing work to CC. Decides within-sprint operational matters using standing rules. Runs the loop **without Clau** by default. |
| **Claude Code (implementer)** | Decision role inside a task — implementation approach, code-level design, refactors, library use, tests, bug fixes. MUST log every material step + decision so Clau's audit layer has substrate. |
| **Clau (architect)** | Implementation strategy + cross-sprint sequencing. Audit-AFTER, not approval-before. Runs at sprint boundaries, on request, or when something looks off. |

---

## Source of truth (cutover 2026-05-25)

| What | Where | NOT here |
|---|---|---|
| Task board / sprint rows | `.viabe/sprint/VT-<N>.md` | Notion ViabeTeam_Sprint (read-only archive) |
| Session log entries | `docs/clau/entries/CL-<N>.md` | Notion Clau_Session_Log (read-only archive) |
| Standing decisions | `docs/clau/decisions-ledger.md` | — |
| Latest snapshot | `docs/clau/latest-snapshot.md` | — |
| Active-context digest | `docs/clau/active-context-summary.md` | — |
| Launch milestones | `.viabe/launch-tracker.md` (Cowork-managed) | Notion `Viabe_Launch_Tracker` (archival) |
| WhatsApp template registry | `.viabe/templates.md` — canonical `template_name → SID` map | hard-coded SIDs in code (none allowed) |
| Discipline rules | `docs/clau/discipline-rules.md` | — |
| Operating brief | `docs/clau/operating-brief.md` | — |

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
| Open PRs | `gh pr list --state open` |
| The next VT-ID for a new row | `python scripts/vt_id_allocate.py --peek` (consume: drop `--peek`) |
| Current dashboard | open the Cowork artifact `viabe-team-pm-dashboard` (Cowork sessions only) |

---

## The autonomous Cowork ↔ CC loop

This is how delivery actually happens between Fazal-issued scope grants. Fresh sessions miss this if they only read the role table.

### Signal pipeline

| Direction | Inbox |
|---|---|
| Cowork → CC | `.running/to-claudecode/` |
| CC → Cowork | `.running/to-cowork/` |
| Archive | `.running/processed/` |

**Signal types:** `brief-ready`, `task`, `task-merge`, `review`, `addendum`, `question`, `answer`, `task-result`, `merged`, `pr-open`, `status`. Schema in `.viabe/protocol.md`.

**Required frontmatter:** `from`, `to`, `type`, `ts`, `session_blanket_auth: true|false`, `authorized_by: fazal` (only when Fazal explicitly granted), `authorization_basis: "<quoted Fazal directive + timestamp>"`. Briefs additionally require `cl_decisions_checked: [CL-N, ...]` per Rule #16; CC bounces missing-field signals.



### Orchestrator modes

CC runs under one of two orchestration modes:

- **Interactive `claude -c` watch loop** — Fazal's primary today. Opened in a
  terminal, left running. Watches `.running/to-claudecode/` and processes
  signals as they arrive. Live console output is visible to Fazal.
- **Python daemon at `.viabe/daemon/`** — installed but paused via
  `.viabe/daemon/STOP` file by default. Background process. Same watch
  semantics; no live console.

Cowork side runs scheduled pollers:

- `viabe-team-queue-poller` — every 15 min (was 3 min before 2026-05-30
  canonical migration). Watches `.running/to-cowork/`, triages, surfaces
  to Fazal. Pillar 7 binding; no auto-merge.
- `viabe-team-dashboard-regen` — every 10 min. Regenerates the Cowork
  dashboard artifact from sprint/CL state.

If both interactive and daemon are running at once they'll race on the same
inbox. Pick one. Default: interactive watch loop on canonical machine;
daemon STOPped.


### Self-triggered polling

Whenever CC has open work, **poll `.running/to-cowork/` + `git rev-parse HEAD` continuously without waiting for Fazal to say "check CC."** Don't stop at 3 minutes of quiet. The scheduled poller fires in a different session and won't land here. Keep polling until CC signals task-result or Fazal redirects.

### Session-blanket auth model

Fazal grants scope at **batch level** ("ship batch 9," "complete the queued task"). Within that grant:

- Cowork dispatches briefs + runs reviews + signals task-merge autonomously
- CC implements + opens PRs + merges per Pillar 7 task signals
- Neither asks Fazal "should I proceed?" for in-scope steps

**New scope = new explicit grant.** You don't ask mid-batch for every step, but you also don't widen scope without asking.

### Merge workflow (post-VT-245, 2026-05-30)

Main protection is an account-level ruleset, but **"Require status checks to pass" is OFF** (Fazal, 2026-05-30) — CI no longer gates merges. The **local pre-push hook is the safety gate**; run `scripts/install-hooks.sh` once after cloning. CI is a non-blocking backstop on PRs.

- **Before every push:** the `pre-push` hook runs the fast CI-equivalent suite (ruff + dep-less smoke + team-web tsc/vitest/lint + a conditional orchestrator docker build). It aborts the push on failure. Bypass with `git push --no-verify` (sparingly). Never push code the hook (or the equivalent local commands) hasn't passed — failing CI burns Actions minutes.
- **Trigger-diet (VT-245):** ci.yml + deploy-dev.yml `paths-ignore` docs/sprint/session/cross-workflow changes, so those PRs/merges run 0 jobs.
- Route-via-PR remains the convention (not enforced).
- Recurring flakes (being fixed in VT-245): RLS service_count + chrono-order in `test_pipeline_log.py` — rerun via `gh pr checks <N> --watch` if they trip pre-fix.

### Deploy topology (ground truth, 2026-05-30 — check this BEFORE debugging a failing deploy)

A `railway up` CLI job was debugged for an hour this session chasing `RAILWAY_TOKEN` — it was redundant with Railway's native auto-deploy, and its failure was *blocking* that native deploy. Root cause: the topology lived only in the Railway dashboard. So, the ground truth:

- **Orchestrator (`apps/team-orchestrator`)** → **Railway NATIVE GitHub auto-deploy**. The Railway service is connected to `rkecom-in/viabe-team`, branch `main`, "Auto deploys on push" ON, "Wait for CI" ON. **There is NO `railway up` in CI** (the redundant job was removed in VT-246 / #154). Railway redeploys itself once `deploy-dev` is green. No `RAILWAY_TOKEN` in CI (secret unused/deletable).
- **team-web (`apps/team-web`)** → **Vercel CLI job in `deploy-dev.yml`** (`vercel pull/build/deploy --prebuilt`, `VERCEL_TOKEN`; runs at repo root, project root-dir = `apps/team-web`; needs pnpm via `pnpm/action-setup`). Triggers on push to main.
- **`deploy-dev.yml`** = `pre-deploy-checks` + the Vercel job only. Because Railway's "Wait for CI" skips the native deploy if ANY Action on the push fails, **keeping `deploy-dev` green is what lets the orchestrator deploy** — a red CI run silently blocks it.
- **Discipline:** before "fixing" a failing deploy/CI step, check this topology first — don't repair a step that's redundant with platform-native config (Railway/Vercel dashboards own the actual deploy).

### FUSE lock workflow

The sandbox cannot unlink `.git/index.lock` files left by interrupted writes — FUSE mount denies the operation. Only Fazal's native Mac terminal can `rm` the lock.

When sandbox `git add` fails with `fatal: Unable to create '.git/index.lock': File exists` — **signal Fazal, don't retry, don't workaround**. He runs `rm /Users/fazalkhan/development/viabe-team/.git/index.lock` from terminal in under 5 seconds.

---

## Standing disciplines (full text in `docs/clau/discipline-rules.md`)

**Rule #14 — reconcile against ground truth.** Every status summary, sprint order, or handoff is reconciled against `gh pr list --state merged` + the log files before trusted. Memory is never authoritative. Applies to Clau's summaries too. **The snapshot itself drifts and is subject to this rule** — treat it as a starting hypothesis until git log confirms.

**Rule #15 — canary mandatory.** Every brief touching external API / SDK / persistence MUST include a canary acceptance step. Real API call, verify response, fail-not-skip on error. Cowork bounces plan-ready signals without canary plans.

**Rule #16 — pre-dispatch ledger scan.** Before Cowork dispatches any `brief-ready` signal, run `python3 scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md` and add `cl_decisions_checked: [CL-N, ...]` to the signal frontmatter listing every active-context row the script surfaced. CC bounces brief-ready signals missing that field. Triggered by VT-101 LangSmith drift; substrate is `docs/clau/active-context-summary.md`.

**Rule #17 / CL-418 — shared git index.** Single working tree shared across Fazal + Cowork + CC + Claude chat. CC must NOT `git stash --include-untracked` (-u). CC must use explicit `git add <files>`, NOT `git commit -am`. Working-tree obstacles → signal Cowork + wait; don't workaround. Triggered by VT-30 + VT-178 sweep recurrence.

**Pillar 7 — Fazal-authorized merges.** Every PR merge requires `type: task` with `authorized_by: fazal`. Never auto-merge. Session-blanket auth is grant-scoped, not perpetual.

**CL-421 (Locked Standing, 2026-05-29)** — ALL Integration Agent connectors MUST be zero-manual-paste after OAuth. Triggered by VT-212 Apps Script paste step being customer-hostile for the Tier-2/3 SMB persona.

**CL-422 (Standing with launch-gate sunset, 2026-05-29)** — Dev Supabase project in `ap-northeast-2` (Seoul) is accepted. Prod = Mumbai (VT-231 launch-blocker). Hard constraint: **NO real customer data on dev until VT-231 closes.** Do not re-flag Seoul as a DPDP issue.

**Exec Order first.** Within-sprint ordering = sort by `exec_order` then VT-N. Not Priority. Not dep-graph guesswork. Read the brief's Dependencies section explicitly.

**VT-IDs numeric only.** Never invent text-suffix IDs like `VT-FOO`. Allocator at `scripts/vt_id_allocate.py` claims monotonic numeric IDs under flock.

**Don't re-litigate Standing decisions.** If it's in the ledger, it's settled.

**Before asking Fazal anything,** state what you checked (snapshot + ledger + active-context). Bare questions get bounced.

**Dashboard is light-mode only** — hard CSS lock in the Cowork artifact.

---

## What's notably NOT here

- **`docs/clau/resurrection-file.md`** is missing — Clau owes a dump. Not blocking but it's the deep-context file for fresh Clau sessions.
- **Discipline rules #6, #7, #10, #11** are partially TODO in `discipline-rules.md` (6 TODO/TBD markers as of last audit). The migration extracted 10 of 14 from session log entries; the rest are paraphrased.

---

## How NOT to behave

- **Don't trust the snapshot's HEAD / IN FLIGHT without reconciling against `git log` first.** Drift is common. (Rule #14 anti-pattern.)
- **Don't re-derive what the snapshot already says** — once reconciled. If `latest-snapshot.md` says the critical path is X and `git log` confirms, that's the answer.
- **Don't trust your own memory across sessions** — auto-memory at `~/Library/Application Support/Claude/.../spaces/<id>/memory/` is **per-space**. A new Cowork window, a Dispatch thread, or a phone session does NOT see it. The repo files are the only cross-space substrate.
- **Don't roster a new VT row without using the allocator** (`scripts/vt_id_allocate.py`). The Notion `auto_increment_id` is gone; the file counter at `.viabe/sprint/.next-id` is the replacement.
- **Don't write to Notion.** It's a read-only archive. Every Cowork/CC/Clau write goes to the `.viabe/sprint/` or `docs/clau/` files.
- **Don't push directly to main.** Branch protection rejects it. Route via PR, plan CI time.
- **Don't auto-merge.** Pillar 7 requires Fazal authorization per merge.
- **Don't dispatch a filesystem-blocked task to a different agent without checking if the blocker is shared.** It usually is (CL-418). Sandbox FUSE lock issues hit CC the same way they hit Cowork.
- **Don't ask "should I proceed?" inside a session-blanket auth window** for in-scope work. Proceed and signal status.
- **Don't widen scope beyond the granted batch** without asking.
- **Don't echo Fazal's framing back at him.** Stress-test first, agree later if warranted. No glazing.
- **Don't summarize at end-of-response.** Fazal reads the diff.

---

## Tone preferences (Fazal)

Push back first, agree second. Lead with what's wrong or missing. Be direct and concise. Skip warm-ups. Call out weak logic and blind spots especially when Fazal sounds certain. Agreement must be earned with reasoning, not offered as a default.

---

## If something is unclear

Per Rule #14: check the snapshot + ledger + active-context-summary + `git log` first, then ask. Don't ask without stating what you checked.

Cross-refs deeper than this file:
- Signal protocol detail: `.viabe/protocol.md`
- Brief audit history: `docs/clau/entries/CL-322.md`, `CL-386.md`, `CL-389.md`, `CL-390.md`, `CL-418.md`, `CL-421.md`, `CL-422.md`
- Migration story: `docs/clau/operating-brief.md` §3
- Sprint board schema: `.viabe/sprint/README.md`

