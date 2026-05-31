# Latest State Snapshot

**As of:** 2026-05-31 (regenerated against current main per VT-242; reconciled against `git log --oneline -25` + `gh pr list --state merged` + `.viabe/sprint/*.md` statuses).
**Main HEAD:** `c85a52f` (PR #189 VT-197 Day-39 → L1 reflection loop). **Reports-Jun15 in 15 days.**

> Reconciled live against ground truth. The prior snapshot ("four held PRs #143/#144/#145/#146, HEAD `65244b7`") is fully stale — all four merged plus ~40 more rows landed since. Nothing in flight on CC; no PRs held for Fazal this session.

---

## CRITICAL PATH

The **cost + moat levers are complete and live on main.** VT-194 (prompt caching, 10x cost lever) + VT-195 (L1 tenant-context substrate — per-tenant knowledge graph + Context Composer read path + L1 block pre-injected at dispatch after the cached prefix + business_profile writer + RKeCom seed) + VT-197 (Day-39 evaluator → context-bundle reflection loop, closing the learning cycle) all merged. SR-Agent substrate is done: customers + `campaign_recipients` cohort integrity (VT-170), the attribution write-path substrate (VT-240) + cohort→collapse wiring with Fazal's reject-behavior ruling (VT-241), the SR-agent E2E harness + 3 loop fixes (VT-140), campaign execution seam (VT-251), and the security sweep (VT-252/253/254 — session-export secret-leak prevention + RLS set_config tenant-scoping). Remaining pre-launch work is the **Actions-diet CI levers (VT-259 per-path job gating, VT-260 pre-push DB coverage), owner business-profile onboarding (VT-267), and the business-profile guardrail enforcement (VT-268)**. Launch-blocker remains VT-231 (prod Supabase Mumbai, Fazal-side; CL-422 gate — no real customer data on dev until it closes).

## IN FLIGHT

**Nothing on CC.** No held PRs this session; the merge backlog cleared (PRs #165–#189 all landed). One stale open PR remains:
- **#61 — VT-172** (chore/rescue uncommitted migration output + Cowork operating layer) — open since 2026-05-26, predates the canonical-machine migration. Reconcile or close; not in active flight.

CC is idle awaiting the next brief/task signal.

## BLOCKED ON

**Fazal-side launch cluster:** VT-231 prod Supabase Mumbai (launch-blocker, CL-422 — no real customer data on dev until it closes). Vendor approvals VT-108/109/110/111/112/113/114/115 (Meta templates / Razorpay Live / Apify / Twilio DLT / KYC / Resend DMARC / LangSmith billing / DPDPA review — Fazal/vendor-side). VT-44/45-send-path and WhatsApp delivery remain vendor-gated on Meta templates + Twilio DLT.

## NEXT ACTION

(1) Land the Actions-diet CI levers: **VT-259** (per-path job gating — skip web jobs for orchestrator-only PRs and vice versa) + **VT-260** (pre-push hook DB coverage — run orchestrator + migrations jobs vs local pg). Both Medium, follow-ons to VT-245/VT-255. (2) **VT-267** owner business-profile onboarding (Integration Agent → identity) + **VT-268** business-profile guardrail ENFORCEMENT (policy ≠ context) — both High; VT-268 depends on VT-267's profile substrate. (3) Reconcile/close stale PR #61 (VT-172). (4) Continue the Integration Agent connectors (VT-208 Shopify in progress; CL-421 zero-paste discipline binds).

## DO NOT

Do NOT re-litigate Standing decisions: CL-421 (zero-manual-paste connectors after OAuth), CL-422 (Seoul dev DB accepted; no real customer data on dev until VT-231), CL-423 (every PR title ends in a real numeric `(VT-<N>)`), CL-424 (xhigh ultracode + fan-out; allocate VT-IDs/migration numbers ONCE up-front via the flock-serialized allocators, never race them). Do NOT push to main directly / auto-merge (Pillar 7 — every merge needs Fazal `task-merge`). Do NOT trust this snapshot's HEAD without reconciling against `git log` (Rule #14). Do NOT hand-pick migration numbers — use `scripts/migration_id_allocate.py`.

---

## Recently landed (this session, 2026-05-30 → 05-31)
- **VT-194** — prompt caching for orchestrator-agent (Anthropic cache_control; 10x cost lever).
- **VT-195** (Done) — L1 tenant-context substrate across 4 PRs (#181 design doc, #184 Option-A reframe, #185 Context Composer read path, #187 pre-inject L1 block at dispatch, #188 business_profile writer + RKeCom seed + admin read).
- **VT-197** (Done, #189) — Day-39 evaluator → context-bundle reflection loop (closes the learning cycle).
- **VT-240** (Done) — attribution_method/confidence write-path substrate; lifts VT-43's cohort_size/attribution_rate.
- **VT-241** (Done) — `resolve_cohort_recipients` wired into the campaign collapse path; Fazal's reject-behavior ruling applied.
- **VT-251/261/262/263** — campaign execution seam + opt-out skip status + idempotency hardening + RLS test hardening.
- **VT-252/253/254** — security: gitignore raw session-export dirs, CI guard rejecting URL auth-token values, RLS set_config tenant-scoping sweep + real-DB denial tests.
- **VT-255** — CI Actions-diet: split pr-title workflow + drop `edited` trigger.
- **VT-256/257/264** — tool-schema drift reconciliation (`get_recent_campaigns` + `query_customer_ledger`) + narrowed UndefinedColumn catch.
- **VT-140** (#175) — Sprint 1+2 SR-agent E2E harness + 3 real loop fixes.
- **VT-243** — sprint board-hygiene reconciliation to git ground-truth.

## Pending rows (next up)
- **VT-259** (Backlog, Medium) — per-path CI job gating.
- **VT-260** (Backlog, Medium) — pre-push hook DB coverage vs local pg.
- **VT-267** (Backlog, High) — owner business-profile onboarding (Integration Agent → identity).
- **VT-268** (Backlog, High) — business-profile guardrail ENFORCEMENT (policy ≠ context); depends on VT-267.
- **VT-208** (In Progress) — Shopify connector (Admin API access token).

## Operating reality (2026-05-31)
| Fact | Detail |
|---|---|
| Canonical machine | Single canonical instance (repo + ~/.claude). CL-418 shared-tree applies. |
| Git writes via CC | All git/terminal writes go to CC (full native access); Cowork sandbox is read-only (read-only PAT, FUSE blocks index.lock). Fazal only on no-other-option. |
| Queue poller | `viabe-team-queue-poller`, */15. Triages to-cowork, flags drift, never merges/dispatches (Pillar 7). |
| dashboard-regen | Regenerate on demand via the build scripts → artifacts `viabe-team-pm-dashboard` / `viabe-team-sprint-dashboard`. |
| CI gate | Main protection ruleset present but "Require status checks to pass" OFF (VT-245). Local pre-push hook is the safety gate; CI is a non-blocking backstop. |
| Ledger anchor | Latest Standing decisions: CL-421/422/423/424. Decisions-ledger reconciled to source CL entries. |

## How to read this snapshot
Read after `operating-brief.md`. Hypothesis until `git log` confirms HEAD (Rule #14). The poller flags drift but does not regenerate this file — regeneration is Fazal-authorized (filed as VT-242 this pass).
