# Latest State Snapshot

**As of:** 2026-05-30 (Cowork-authored, Opus session on the new canonical machine; reconciled against `git fetch` + `git log origin/main`).
**Main HEAD:** `9f2f38d` (PR #141 session export; = `origin/main`, verified in sync). **Reports-Jun15 in 16 days.**

> Reconciled live: local HEAD == `origin/main` == `9f2f38d`. The prior snapshot was stale at `d8a3e20` and claimed VT-40/41/42/46/238 in flight — all of those merged (PRs #135–139) plus the two session-close PRs (#140, #141). Zero in flight at this baseline.

---

## CRITICAL PATH

Sprint 2 substrate is shipped and the Ops Console works end-to-end. The 2026-05-29 marathon cleared the Sprint 2 Integration Agent epic, the non-Razorpay parts of Sprint 2 Cost+Moat, the Sprint 9 docs cluster, all Fazal-surfaced auth hot-fixes (VT-230/232/233/235/236), env-password auth (VT-237), the 4 SR-Agent MCP tools (VT-40/41/42/46), and the terminal-stream redesign (VT-238). Auth is live: magic-link OR env-password → 7-day operator JWT cookie → styled Ops Console. Customer-data substrate is consent-gated + RLS-isolated. **Remaining path to Reports-Jun15:** the launch-blocker is VT-231 (prod Supabase in Mumbai) — Fazal-side, and the CL-422 hard constraint means no real customer data touches dev until it closes. Everything else toward launch is either ship-thin feature work (batch 11 candidates below) or vendor/Fazal-blocked.

## IN FLIGHT

**Nothing.** Zero open Cowork→CC work items at this baseline. `.running/to-cowork/` is empty; the dangerous stale `20260530T002000Z-task-session-close-commit-substrate` signal was retracted (`_RETRACTED-` in `processed/`). `.running/to-claudecode/` holds 67 already-handled signals awaiting an archive sweep (hygiene, not work). No PR is open that this session can see — note CI/PR-state is not checkable from the Cowork sandbox (see Operating reality).

## BLOCKED ON

VT-231 prod Mumbai provisioning (Fazal-side, **launch-blocker** — provision viabe-team-prod Supabase in ap-south-1 with migrations + secret rotation + Railway/Vercel prod wiring; CL-422 gate). VT-225 design-doc review (Fazal-side — L0 per-tenant k-anonymity admission doc shipped PR #128, awaits review before implementation). VT-228 dynamic operator allowlist (deferred — touches the VT-237/233/236 auth surface; ship after VT-237 is verified working in Vercel). Sprint 8 launch cluster (Fazal/vendor-blocked — Razorpay Live KYC, Twilio DLT, landing copy). Vendor approvals VT-108/109/111/113/114/115 (Meta templates, Razorpay Live, Twilio DLT, Apify, Resend DMARC, LangSmith billing, DPDP final review).

## NEXT ACTION

Moving to implementation (batch 11). Top candidates, ship-thin toward Reports-Jun15: VT-228 (dynamic operator allowlist — now that VT-237 env-password is shipped), VT-225 implementation (after Fazal reviews the design doc), remaining SR-Agent MCP tools VT-43/44/45/47/48 (standalone-callable like the VT-40/49 pattern; check substrate gates per Rule #16), VT-189 Ops Console V2 sub-row decomposition, and Sprint 8 rows that don't need Razorpay Live yet (VT-87 read-only owner portal, VT-86 monthly impact-report PDF substrate). Confirm Fazal has set `OPERATOR_EMAIL` + `OPERATOR_PASSWORD` in Vercel before leaning on password login.

## DO NOT

Do NOT re-litigate Standing decisions in `docs/clau/decisions-ledger.md` — **CL-421** (zero-paste connectors, Locked) and **CL-422** (Seoul dev DB accepted; Mumbai = prod; no real customer data on dev until VT-231) are settled. Do NOT re-architect VT-237 env-password auth (Fazal's explicit choice; migrates to per-operator hashes when VT-228 ships). Do NOT roll back VT-235 card styling for the terminal redesign — VT-238 only touched `/team/ops/stream`. Do NOT push directly to main (protected, 11 checks; route via PR). Do NOT auto-merge (Pillar 7). Do NOT trust this snapshot's HEAD without re-running `git fetch` + `git log origin/main` (Rule #14).

---

## Operating reality (new this session — 2026-05-30)

| Fact | Detail |
|---|---|
| **Canonical machine migration done** | New machine is the single canonical instance; repo + `~/.claude/` copied over, old machine retired. Shared-tree discipline (CL-418) now applies here. |
| **Queue poller scheduled** | `viabe-team-queue-poller`, `*/15`, git-only (CI-blind). Triages `.running/to-cowork/`, reconciles via git, flags snapshot drift; never merges or dispatches (Pillar 7). |
| **dashboard-regen NOT scheduled** | Fazal dropped it — dashboards regenerate on demand via `scripts/build_dashboard.py` + `build_sprint_dashboard.py` → artifacts `viabe-team-pm-dashboard` / `viabe-team-sprint-dashboard` (re-registered this session). |
| **Sandbox git access** | Repo is **private**; read-only PAT in `.git/config` remote URL. `git fetch`/`log`/`ls-remote` work. `gh` not installed; `api.github.com` blocked → no CI/PR-state from sandbox (terminal-side only). |
| **Pending PR** | CLAUDE.md v2 (incl. public→private fix) + this snapshot regen staged for a docs PR Fazal pushes terminal-side (Cowork token is read-only). |

## How to read this snapshot

Per the bootstrap order, this is read after `operating-brief.md`. It is a hypothesis until `git fetch` + `git log origin/main` confirm the HEAD (Rule #14). The `viabe-team-queue-poller` flags drift but does not regenerate this file — regeneration is a Fazal-authorized step.
