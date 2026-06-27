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

**Viabe Team** — a multi-agent system for small Indian business owners. WhatsApp-first, owner-facing portal at viabe.ai/team. A Python+Next.js monorepo with **two deployable apps today** (orchestrator + web); the ingestion worker is a Phase-1 scaffold, not deployed:

- `apps/team-orchestrator/` (Python 3.13, DBOS + LangGraph + Anthropic SDK) — critical path
- `apps/team-web/` (Next.js 16, React 19) — webhooks + marketing + dashboard + Ops Console
- `apps/team-ingestion-worker/` (Python 3.13, Apify + Sarvam) — **Phase-1 `SystemExit` scaffold (VT-17), NOT deployed.** Platform ingestion actually runs IN the orchestrator via the apify method path (`integrations/methods/apify_gbp.py`, `apify_food.py` → `business_profile` aggregate + VT-325 `platform_listings` per-listing rows), not in this worker.
- `packages/team-shared/` (cross-app types)

**Positioning (Fazal 2026-06-13 — CL-440, carry this every session):** **Viabe Team is the core product** — the autonomous AI business platform that runs business tasks for owners. **Viabe Reports is an awareness feature**, not a co-equal product (top-of-funnel: build the Viabe brand, attract founders). In all external/positioning contexts, Reports is referred to as **"Viabe's Location Feasibility Report"**; Viabe Team stays **Viabe Team**. Reports remains a technically separate codebase/DB/KG — "feature" is the market framing, not a repo merge.

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
| The next migration number | `python scripts/migration_id_allocate.py --peek` (consume: drop `--peek`) — **MANDATORY for new migrations; never hand-pick the number** (CL-424) |
| Current dashboard | open the Cowork artifact `viabe-team-sprint-dashboard` (the ONE dashboard — full filterable per-row board; the PM dashboard was retired, CL-430) |

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

**On every (re)start, FIRST read [`.viabe/cc-startup-protocol.md`](.viabe/cc-startup-protocol.md) and run its startup sequence in order** — return the tree to clean `dev` → drain the FULL inbox oldest-first → reconcile vs `git log origin/dev` (Rule #14) → announce liveness — before any new work. In-session watchers/crons DIE on a process restart and do not self-recover; that protocol is the recovery discipline (archive signals only AFTER execution; re-arm-first on every wake; heartbeat >10min tasks). Cowork-mandated 2026-06-24 after two restart-stalls.

CC runs under one of two orchestration modes:

- **Interactive `claude -c` watch loop** — Fazal's primary today. Opened in a
  terminal, left running. **On startup, FIRST drain the inbox: process every
  signal already in `.running/to-claudecode/` oldest-first (a signal sitting
  there at launch is NOT an "arrival" and the watcher won't fire on it), THEN
  enter watch mode** for new arrivals. Live console output is visible to Fazal.
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

### Merge workflow (post-VT-245, 2026-05-30; dev-branch governance VT-363/CL-432, 2026-06-09)

**Branch governance (VT-363, Fazal 2026-06-09 — CL-432):** there are TWO long-lived branches.
**`dev`** → Railway Dev + Vercel `viabe-team-web-dev` + Supabase Seoul (the deployed, E2E-tested env).
**`main`** → Railway Prod + Vercel `viabe-team-web` + Supabase Mumbai (deliberate, real customer data).
- **CC's default PR base is `dev`.** CC self-merges [BUILD] rows on green into **`dev`** (CL-429, now → dev); risk rows (money/auth/PII/RLS/classifier) still get the Cowork subagent gate before the dev merge.
- **`main` is Fazal-authorized ONLY.** CC NEVER merges to `main` without an explicit Fazal `type: task` promotion instruction (the new Pillar-7 gate). A `dev`→`main` promotion PR opens only on Fazal's word; Cowork relays it. A PR targeting `main` is forbidden unless it's that authorized promotion.
- Flow: feature branch → PR into `dev` → CC self-merge on green → Dev deploy → phase E2E → **Fazal authorizes dev→main promotion** → Prod.

Main protection is an account-level ruleset, but **"Require status checks to pass" is OFF** (Fazal, 2026-05-30) — CI no longer gates merges. The **local pre-push hook is the safety gate**; run `scripts/install-hooks.sh` once after cloning. CI is a non-blocking backstop on PRs.

- **Before every push:** the `pre-push` hook runs the fast CI-equivalent suite (ruff + dep-less smoke + team-web tsc/vitest/lint + a conditional orchestrator docker build). It aborts the push on failure. Bypass with `git push --no-verify` (sparingly). Never push code the hook (or the equivalent local commands) hasn't passed — failing CI burns Actions minutes.
- **Trigger-diet (VT-245):** ci.yml + deploy-dev.yml `paths-ignore` docs/sprint/session/cross-workflow changes, so those PRs/merges run 0 jobs.
- Route-via-PR remains the convention (not enforced).
- **CC MAY PUSH `origin/dev` WHEN REQUIRED (STANDING, Fazal 2026-06-28 — supersedes the 06-27 explicit-push rule).** Fazal granted CC push authority to `origin/dev`; no longer wait for a Fazal-explicit "push". Guardrails that REMAIN (cost control was the original reason — keep it sane): push only at a **DEPLOYABLE CHECKPOINT** (a coherent, green, gate-passed unit — NOT per-change), **BATCH** commits into ONE push, the **pre-push hook must be green** (`--no-verify` only for the known pre-existing l2_send lint), and `main` stays **Fazal-only**. "When required" = judgment: a checkpoint worth deploying, not every commit. Don't wait on Cowork for the push either — Cowork gates risk rows before the checkpoint, but CC owns the push. See `decisions-ledger.md` (CL-2026-06-28-push-authority) + `.viabe/cc-startup-protocol.md`.
- Recurring flakes (being fixed in VT-245): RLS service_count + chrono-order in `test_pipeline_log.py` — rerun via `gh pr checks <N> --watch` if they trip pre-fix.

### Deploy topology (ground truth, 2026-05-30 — check this BEFORE debugging a failing deploy)

A `railway up` CLI job was debugged for an hour this session chasing `RAILWAY_TOKEN` — it was redundant with Railway's native auto-deploy, and its failure was *blocking* that native deploy. Root cause: the topology lived only in the Railway dashboard. So, the ground truth:

- **Orchestrator (`apps/team-orchestrator`)** → **Railway NATIVE GitHub auto-deploy**. The Railway service is connected to `rkecom-in/viabe-team`, branch `main`, "Auto deploys on push" ON, "Wait for CI" ON. **There is NO `railway up` in CI** (the redundant job was removed in VT-246 / #154). Railway redeploys itself once `deploy-dev` is green. No `RAILWAY_TOKEN` in CI (secret unused/deletable).
- **team-web (`apps/team-web`)** → **Vercel NATIVE Git auto-build** on push to `dev` (Fazal re-enabled "auto-build on push" for the `viabe-team-web-dev` project, 2026-06-27). The old Vercel CLI `--prebuilt` job in `deploy-dev.yml` was **REMOVED** (it double-built AND didn't auto-attach the production alias = the stale-URL bug). `deploy-dev.yml` is now `pre-deploy-checks` ONLY (Railway's "Wait for CI" still gates the orchestrator deploy on it being green). Native auto-build auto-attaches the production alias to the latest deploy. Now-unused secrets: `VERCEL_TOKEN`/`VERCEL_ORG_ID`/`VERCEL_PROJECT_ID`.
- **`deploy-dev.yml`** = `pre-deploy-checks` + the Vercel job only. Because Railway's "Wait for CI" skips the native deploy if ANY Action on the push fails, **keeping `deploy-dev` green is what lets the orchestrator deploy** — a red CI run silently blocks it.
- **Discipline:** before "fixing" a failing deploy/CI step, check this topology first — don't repair a step that's redundant with platform-native config (Railway/Vercel dashboards own the actual deploy).

#### Two-environment model (2026-06-09 — Fazal granted CC direct console access)

The single-env mechanics above describe the **Dev** path. The full topology is now **Dev + Prod**:

- **Railway** — ONE project, TWO environments: **Dev** ← `dev` branch + **Prod** ← `main` branch (decided VT-363/CL-432). Hosts the orchestrator (`vt-orchestrator-service`). `EXPECTED_ENV=dev` set on Railway Dev (VT-362 guard); `EXPECTED_ENV=prod` Fazal-set on Prod. The env→branch trigger itself is a Railway-console setting (not CLI-inspectable) — verify/set it in the dashboard.
- **Vercel** — TWO projects: **`viabe-team-web-dev`** (exists, dev) + **`viabe-team-web`** (PROD, **not yet created** — creation + deploy wiring = VT-231 cutover scope). Hosts team-web. **Name-collision note:** the Vercel projects are `viabe-team-web-dev` / `viabe-team-web` — distinct from the **Supabase** dev project `viabe-team-dev` (a different service; ground truth = `apps/team-web/.vercel/project.json` → `viabe-team-web-dev`). Do not conflate them.
- **Supabase** — TWO projects: **dev = Seoul (`ap-northeast-2`)**, **prod = Mumbai (`ap-south-1`, being created)**. ADR-0003 dual-project + [[CL-422]] (dev-Seoul accepted; NO real customer data on dev until VT-231). Fazal chose two separate projects over single-project-branching (2026-06-09).
- **CC console access (2026-06-09):** CC has direct management access to the **Supabase** (both projects), **Railway**, and **Vercel** consoles — Fazal logged CC in. CC can read/set env vars directly, **subject to the authority gate below (CL-431).**
- **Authority gate (CL-431):** CC manages **DEV** env vars autonomously; **every PROD env-var change (config OR secrets) requires explicit Fazal authorization first** — the Pillar-7 analog for infrastructure. **Secrets hygiene (binding):** CC NEVER writes a live secret VALUE into any repo signal/log/PR/commit (the repo is git — an echoed secret is a committed secret); CC sets/rotates in the console and reports ONLY the variable NAME + action. **By-reference, never by value:** for any prod secret CC sets, pipe/substitute the value in a subshell (`railway variables set KEY="$(<source-cmd>)"`) — CC is FORBIDDEN from running anything that echoes a secret to stdout (`cat`/`echo`/`print`/get-variable-plaintext), because CC is a Claude model and plaintext in its context goes to Anthropic that turn. **Supabase PROD creds:** Fazal sets the VALUE; CC never READS the plaintext (no `cat`/`echo`/`print`/open), but a process CC LAUNCHES may CONSUME it from an injected env — `railway run --environment <prod> python …/apply_migrations.py` flows the value OS-env→process, never into CC's context — that's how CC runs prod migrations without knowing the credential (reports only the result + var NAME). **A prod migration run is itself Fazal-authorized** (prod-impacting, Pillar-7 spirit). **Env isolation (dev/prod must never jumble):** always pass `railway --environment <dev|prod>` explicitly (never the linked default); never source `supabase-dev.env` in the same shell as a prod-injected run (ambient dev `DATABASE_URL` would shadow the injected prod one); `apply_migrations.py` is guarded — it refuses unless the connected DB matches an explicit `--expected-env` (VT-362 `app_environment` sentinel); no seed data ever against `--expected-env prod`. See `decisions-ledger.md` CL-431.

### FUSE lock workflow

The sandbox cannot unlink `.git/index.lock` files left by interrupted writes — FUSE mount denies the operation. Only Fazal's native Mac terminal can `rm` the lock.

When sandbox `git add` fails with `fatal: Unable to create '.git/index.lock': File exists` — **signal Fazal, don't retry, don't workaround**. He runs `rm /Users/fazalkhan/development/viabe-team/.git/index.lock` from terminal in under 5 seconds.

---

## Standing disciplines (full text in `docs/clau/discipline-rules.md`)

**Rule #14 — reconcile against ground truth.** Every status summary, sprint order, or handoff is reconciled against `gh pr list --state merged` + the log files before trusted. Memory is never authoritative. Applies to Clau's summaries too. **The snapshot itself drifts and is subject to this rule** — treat it as a starting hypothesis until git log confirms.

**Rule #15 — canary mandatory.** Every brief touching external API / SDK / persistence MUST include a canary acceptance step. Real API call, verify response, fail-not-skip on error. Cowork bounces plan-ready signals without canary plans. **Who runs the canary:** CC runs canary acceptance steps directly — the interactive `claude -c` loop has the Mac's real network egress. Cowork's review sandbox is proxy-blocked from external vendor hosts (e.g. api.sandbox.co.in), so "Cowork couldn't reach the vendor" is NOT a reason to defer a canary to Fazal — dispatch it to CC, which can reach it. Fazal's only input to a canary is the credentials (in `.viabe/secrets/*.env`).

**Rule #16 — pre-dispatch ledger scan.** Before Cowork dispatches any `brief-ready` signal, run `python3 scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md` and add `cl_decisions_checked: [CL-N, ...]` to the signal frontmatter listing every active-context row the script surfaced. CC bounces brief-ready signals missing that field. Triggered by VT-101 LangSmith drift; substrate is `docs/clau/active-context-summary.md`.

**Rule #17 / CL-418 — shared git index.** Single working tree shared across Fazal + Cowork + CC + Claude chat. CC must NOT `git stash --include-untracked` (-u). CC must use explicit `git add <files>`, NOT `git commit -am`. Working-tree obstacles → signal Cowork + wait; don't workaround. Triggered by VT-30 + VT-178 sweep recurrence.

**Rule #18 / VT-403 — env inspection is names→booleans only.** CC NEVER runs raw `railway variables` / `railway variables --json`, and NEVER pipes any secret store (env dump, `.env`, vault) to `head`/`cat`/`echo`/`print`/`jq`/a grep whose match can carry a value, when its output reaches stdout. `railway variables` output carries live secret VALUES; printing it puts a secret in CC's turn context (→ Anthropic that turn) — a CL-431 breach that happened **twice** (`RESEND_API_KEY` golive scan, then a PROD `ANTHROPIC_API_KEY` fragment in the VT-402 Conductor scan). **All env inspection goes through `scripts/env_presence.py`:** `presence --source {env|railway} NAME…` → `NAME: set|unset`; `equal LABEL spec_a spec_b` (specs `env:`/`railway:`/`literal:`) → `LABEL: MATCH|MISMATCH|unset`. It reads values internally but emits only booleans. When a value MUST be consumed (a prod migration, a send canary), flow it OS-env→process by-reference (`railway run …` / subshell substitution) — never into CC's context. Enforced by the `gate-no-raw-railway-variables` CI check. **Binds CC AND Cowork dispatches.**

**Pillar 7 — Fazal-authorized merges.** Every PR merge requires `type: task` with `authorized_by: fazal`. Never auto-merge. Session-blanket auth is grant-scoped, not perpetual.

**CL-421 (Locked Standing, 2026-05-29)** — ALL Integration Agent connectors MUST be zero-manual-paste after OAuth. Triggered by VT-212 Apps Script paste step being customer-hostile for the Tier-2/3 SMB persona.

**CL-422 (Standing with launch-gate sunset, 2026-05-29)** — Dev Supabase project in `ap-northeast-2` (Seoul) is accepted. Prod = Mumbai (VT-231 launch-blocker). Hard constraint: **NO real customer data on dev until VT-231 closes.** Do not re-flag Seoul as a DPDP issue.

**Exec Order first.** Within-sprint ordering = sort by `exec_order` then VT-N. Not Priority. Not dep-graph guesswork. Read the brief's Dependencies section explicitly.

**VT-IDs numeric only.** Never invent text-suffix IDs like `VT-FOO`. Allocator at `scripts/vt_id_allocate.py` claims monotonic numeric IDs under flock.

**Migration numbers via the allocator only.** New migrations MUST claim their number through `scripts/migration_id_allocate.py` (flock-serialized, like the VT-ID allocator) — never hand-pick by scanning `migrations/`. Unlocked directory-scan picking is the recurring collision source under parallel work (e.g. VT-240 + VT-86 both reaching for 047). CL-424.

**Ultracode + parallel fan-out (CL-424).** CC runs xhigh ultracode for all tasks; dynamic-workflow fan-out happens when the orchestrator warrants it. Binding guardrails: (1) allocate every VT-ID and migration number ONCE up-front, before any parallel phase — never let parallel subagents grab IDs/numbers concurrently (both allocators are flock-serialized but the discipline is to assign before fan-out, not race the lock); (2) one coherent PR per numeric VT row regardless of subagent count; (3) Cowork plan-first review still applies on big/risky rows; (4) Pillar-7 Fazal-authorized merge is unchanged.

**Fix agreed issues IMMEDIATELY — no "post-e2e" parking (STANDING, Fazal 2026-06-28).** Once Fazal + Cowork agree something is broken/wrong, build the fix NOW — do NOT roster it as "after the run / post-e2e." The build-discipline is unchanged ("immediately" ≠ "skip the gates"): allocate the VT-id up-front → plan/build → adversarial-verify → Cowork gate → Fazal's explicit origin/dev push → deploy; risk rows (e.g. anything touching the GST verify/gate path) still get the plan-first gate; one coherent PR per row. Only genuinely-new SCOPE (vs an agreed fix) waits for a Fazal grant.

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

