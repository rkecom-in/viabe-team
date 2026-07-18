# VIABE TEAM — OBJECTIVES (status of record)

> Objective-level view — what we are trying to achieve and where each stands. NOT a task list
> (tasks live in the sprint dashboard / VT rows). **Maintainer: CC** (Fazal ruling 2026-07-18
> ~04:15 IST via Cowork 224500Z — this file IS the target checklist; CC updates it at every
> status-changing event, same moment as the to-cowork signal). Cowork audits (Rule #14).
> Bar lines are Fazal's — never edited without a ruling; Now/Gate/status lines are CC's.
> Statuses: MET · MET–INCIDENT OPEN · IN PROGRESS · AT RISK · PENDING · HELD/PARKED.
> Last updated: 2026-07-18 13:20 IST · origin f2f4881 (+3 local: VT-681 phases 3-4, objectives) · cache-fix subagent building.

## O1 · Trustworthy conversation — MET (holding)
- Bar: Tier-1 trust-breakers = 0 across the 10-journey pack ×3; Tier-2 quality ≥ 90%.
- Now: met HEAD-authoritative (Tier-1=0, Tier-2=100%); every brain-touching change re-gates
  against the full pack before trusting.
- Gate: VT-677 ×3 re-drive PASSED (33 runs, ZERO behavioral fails, d9a4e10) — latest full re-proof
  clean. Next re-gate arms on the next brain-touching land (VT-679/680 builds).

## O2 · Money-path integrity — MET, INCIDENT OPEN
- Bar: the Manager can never perform OR claim a money action falsely — DB is sole authority,
  stated values bind to DB, approvals never resolve into silence, corrections revise.
- Now: proven in code/unit/DB asserts (CL-2026-07-16, VT-667/668/670). VT-671 wake-on-signal
  landed + ×3-proven (a57514b): approval resolutions wake the waiting workflow instantly —
  "approved into silence" latency tail dead; first-ever full j01 pass.
- INCIDENT (narrowed, one leg left): diagnosis DONE 2026-07-18. Canary-2 (VT-668 re-arm leg)
  was NOT a breach — conversation_log proved Fazal's own phone approval 13:49 + honest outcome
  report 15:10; live proof PASSED. Canary-1 (customer-list) root-caused → F1–F3 landed 42bd7e6
  (plain-ask delivers, guard copy time-grounded, export_customer_list manager tool).
- Gate to MET: Fazal re-runs canary-1 ("Send me my customer list") — GO sent via Telegram.

## O3 · Agent Capability Framework — MET ON DEV
- Bar: ratified Manager/SubAgent/Tool architecture live — SR + Onboarding on the contract,
  Integration dissolved into Tools, catalog + sufficiency enforced, no un-gated effects.
- Now: VT-101 migration complete + delta-gated on dev; 74-surface catalog; all 4 capability
  gaps closed; flags ON dev.
- Remaining: §7.3 DB-inversion (Fazal-explicit LAST) · prod flag promotion (rides VT-231).

## O4 · Autonomous Manager (management mandate §7) — IN PROGRESS
- Bar: plans (monthly/daily), allocates to specialists, validates outcomes, logs every
  decision with reasons — a manager, not a responder.
- Now: reactive core + delegation contract + outcome-validation + audit log (VT-514) live.
  VT-679 (§7A) + VT-680 (§7C) rostered design-first with question sets — briefs queue after O6.
- Missing: proactive planning loop (§7A) mid-build depth · §7C impact-judge (validate outcome
  quality, not just completion) · dynamic sensing is O9 (held).

## O5 · Owner's language, owner's register — IN PROGRESS (CC-side DONE)
- Bar: every reply and agent-initiated message in the owner's language/register (en/hinglish/hi);
  mirroring wins live turns; preference governs agent-initiated.
- Now: VT-677 CLOSED — all phases landed + ×3 full-journey gate CLEAN (33 runs, hinglish
  journey 3/3, d9a4e10). D1–D3 built as ruled: hi-Latn register, live mirroring never
  overridden, no onboarding question, EN template fallback until Meta approves.
- Gate (last unmet bar item, Fazal/Meta-side): hi-Latn template variants registered with
  Meta — until approved, hinglish owners get EN templates by ruled fallback.

## O6 · Real-tenant reliability — AT RISK (the honest one)
- Bar: what passes on harness tenants must pass on REAL tenants with accumulated state.
- Now: diagnosis re-scored the pattern — canary-2 was HONEST behavior (no false claim; the
  scary read was wrong), canary-1 was a REAL harness-green≠real-green miss (integration state
  hijacked the export route) — fixed 42bd7e6, live re-proof pending (O2 gate). Score: 1 real
  miss, not 2.
- Direction: **VT-682 `--dirty` seed mode DONE + LIVE-PROVEN** — j01 over full dirty residue
  (14d sent campaign, 3d stranded approval, dead-letter task, aged transcript, stale integration
  flow sentinel) = **4/4 PASS on deployed dev**; money path held under dirt (two-gate
  arm-then-send, 8/8 only after explicit confirm). r1 caught + fixed a real instrument hole
  (unfenced late-reply sweep read residue as live money claims) — the fixture earning its keep
  on run one. Clean-vs-dirty is now ONE flag on every journey.
- Gate to lift AT RISK: Fazal's canary-1 re-run (the O2 leg) + a dirty slice folded into the
  standing ×3 gate cadence (roster call: which journeys run dirty by default).

## O7 · Launch readiness (prod) — PENDING
- Bar: prod Mumbai live (VT-231), framework flags promoted, billing ₹5000/agent + per-agent
  trials, Meta template set approved, prod failed-workflow ops (VT-634 — VT-668 is its dev
  seed), signup exposure gate, ownership VTR gate.
- Now: dev-complete substrate; prod not started by design. Waits on Fazal calling VT-231.

## O8 · Learning loop & moat (Track C) — PARKED (post-trust-floor)
- Bar: the Manager learns per-tenant from the audit log; KG/RAG wired; per-capability
  accuracy graduation; concierge as the learning engine.
- Now: substrate exists (audit/trace, L2/L3, KG separate); RAG broker landed but UNWIRED;
  learning loop not started. Deliberately behind the trust floor.

## O9 · Dynamic sensing (Phase 1.2) — HELD (by design)
- Bar: watchers/pollers/listeners the Manager configures; event-driven autonomy.
- Now: spec written + held from CC until Phase 1.1's gate is met (Fazal's sequencing).

## O10 · Phase-1 launch roster — agents + tools READY — IN PROGRESS
- Bar (phase1-plan LOCKED, "Function scope at launch" + ACF §5): every launch agent, function
  mode, and tool live and correctly labelled at Concierge launch — the Manager may promise
  ONLY capabilities marked live for that tenant/environment.
- Agents: **Manager** (embedded) LIVE · **Sales Recovery** LIVE (Concierge; first eligible to
  graduate) · **Onboarding Conductor** LIVE. Advisory functions as Manager-held tools, never
  described autonomous: Marketing (prepare+propose via send rails) · Finance (advisory) ·
  Accounting (prepare-only) · Tech (owner-authorised only) · Cost-Opt (advisory).
- Tools: Shopify OAuth connector ✓ · GST verify gate ✓ · knowyourgst discovery ✓ · Sheets
  zero-paste ✓ · WhatsApp send rails ✓ · common READ set + 74-surface catalog ✓ ·
  customer-list export ✓-built (live canary OPEN) · connector-Tools registry ✓.
- Gaps to the bar: **welcome template Meta UTILITY reapproval** — resubmission pack PREPARED
  (`.viabe/welcome-template-resubmission-package.md`), Fazal runs STEP 0 status check + submits ·
  **hi-Latn template variants** (Fazal/Meta) · export live-canary (fix LANDED 42bd7e6; Fazal
  re-run pending, = O2 gate) · **per-tenant capability registry: VT-681 phases 1–4
  CODE-COMPLETE** (2026-07-18, local commits) — 14-entry promise-relevant registry,
  live/advisory/disabled modes, resolve_for(tenant, env), capability-truth context block at the
  promise seam, D2 net registry-gated (auto-retires on graduation); gate to close = ×3 full-pack
  re-drive (brain-touch, batched with the Fazal-GO cache fixes) · seedable-memory mechanism (C3,
  ships with launch posture).
