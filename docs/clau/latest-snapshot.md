# Latest State Snapshot

**As of:** 2026-05-30 (Cowork-authored, Opus session on the canonical machine; reconciled against `git fetch` + `git log origin/main`).
**Main HEAD:** `65244b7` (PR #142 docs bootstrap refresh; = `origin/main`, verified in sync). **Reports-Jun15 in 16 days.**

> Reconciled live against origin. Four feature/chore PRs are open and **held for Fazal** (see IN FLIGHT) — nothing merged unattended this session.

---

## CRITICAL PATH

Sprint 2 substrate + Ops Console are done (prior session). This session added the **customer-data foundation** the SR-Agent stands on: VT-170 ships the `customers` table + `campaign_recipients` cohort linkage (composite-FK integrity) + tenant registry, which unblocks VT-44 (24h window via `last_inbound_at`), consent gating (`opt_out_status`), and trustworthy attribution cohorts. Two SR-Agent MCP tools (VT-43 read, VT-48 scheduler) and the numeric-VT-row CI gate (VT-239) are also built. **All four are CI-green and held for Fazal's authorization.** Launch-blocker remains VT-231 (prod Supabase Mumbai, Fazal-side; CL-422 gate — no real customer data on dev until it closes).

## IN FLIGHT

**Four PRs open, CI-green, ALL HELD for Fazal `task-merge` (Pillar 7):**
- **#143 — VT-239** numeric PR-title CI gate (rejects text-suffix VT tags; CL-423 standing rule). 18/18 green.
- **#144 — VT-43** `get_attribution_data` (Option A graceful-degrade: cohort/rate/breakdown = None until VT-240). 18/18 green. **Fazal reproducibility gate** before merge.
- **#145 — VT-48** `schedule_followup` + migration 044. 18/18 green.
- **#146 — VT-170** customers + `campaign_recipients` (composite-FK cohort integrity) + registry + redactor wiring, migration 045. Green. Foundational schema — Fazal reviews.

Migrations 044/045 merge **order-independent** (runner tracks by filename in `schema_migrations`, not max-number). No CC work in flight; CC idle awaiting `task-merge` signals.

## BLOCKED ON

**Fazal decisions (none block each other):** (1) reproducibility review of #144; (2) merge authorization for the 4 held PRs; (3) **VT-241 reject-behavior ruling** — when a campaign cohort has an unresolvable/cross-tenant customer id, does collapse fail-closed (reject) or proceed-with-note? Needed before the cohort→collapse wiring ships.
**Standing blockers:** VT-231 prod Mumbai (launch-blocker, CL-422). VT-228 dynamic operator allowlist (after VT-237 verified). Sprint 8 launch cluster + vendor approvals VT-108/109/111/113/114/115 (Fazal/vendor-side). VT-44/45/47 WhatsApp send/approval tools (vendor-gated: Meta templates / Twilio DLT).

## NEXT ACTION

(1) Fazal reviews #144 reproducibility + authorizes the held merges; Cowork dispatches `task-merge` to CC. (2) Fazal rules VT-241 reject-behavior → dispatch VT-241 (collapse wiring). (3) After VT-241, VT-240 lifts VT-43's cohort_size/attribution_rate (reads `campaign_recipients`; do NOT before, or COUNT reads a false 0). (4) Then: VT-44 once Twilio DLT clears, remaining SR-Agent tools, VT-228.

## DO NOT

Do NOT re-litigate Standing decisions: CL-421 (zero-paste connectors), CL-422 (Seoul dev DB; no real customer data on dev until VT-231), CL-423 (all PRs reference a real numeric VT row). Do NOT point VT-43 at `campaign_recipients` until VT-241 populates it. Do NOT push to main directly / auto-merge (Pillar 7). Do NOT trust this snapshot's HEAD without `git fetch` + `git log origin/main` (Rule #14). Do NOT wire the cohort→collapse path until Fazal rules VT-241's reject-behavior.

---

## New rows filed this session
- **VT-239** (built, #143 held) — numeric PR-title CI gate.
- **VT-240** (Backlog) — attribution_method/confidence substrate + VT-43 cohort lift; sequence AFTER VT-241.
- **VT-241** (Backlog) — wire `resolve_cohort_recipients` into the campaign collapse path; carries the Fazal reject-behavior decision.
- **VT-242** (this snapshot-refresh chore row).

## Operating reality (2026-05-30)
| Fact | Detail |
|---|---|
| Canonical machine | New machine is the single canonical instance (repo + ~/.claude copied; old retired). CL-418 shared-tree applies here. |
| Git writes via CC | All git/terminal writes go to CC (full native access); Cowork sandbox is read-only (read-only PAT, FUSE blocks index.lock). Fazal only on no-other-option. |
| Queue poller | `viabe-team-queue-poller`, */15, git-only (CI-blind — `gh`/api.github.com unreachable from sandbox). Triages to-cowork, flags drift, never merges/dispatches. |
| dashboard-regen | NOT scheduled (Fazal dropped it) — regenerate on demand via the build scripts → artifacts `viabe-team-pm-dashboard` / `viabe-team-sprint-dashboard`. |
| Sandbox git | Repo private; read-only PAT in .git/config. `git fetch`/`log`/`ls-remote` work; CI/PR-state not reachable (api.github.com blocked) — terminal-side only. |

## How to read this snapshot
Read after `operating-brief.md`. Hypothesis until `git fetch` + `git log origin/main` confirm HEAD (Rule #14). The poller flags drift but does not regenerate this file — regeneration is Fazal-authorized.
