# VIABE TEAM — OBJECTIVES (status of record)

> Objective-level view — what we are trying to achieve and where each stands. NOT a task list
> (tasks live in the sprint dashboard / VT rows). **Maintainer: CC** (Fazal ruling 2026-07-18
> ~04:15 IST via Cowork 224500Z — this file IS the target checklist; CC updates it at every
> status-changing event, same moment as the to-cowork signal). Cowork audits (Rule #14).
> Bar lines are Fazal's — never edited without a ruling; Now/Gate/status lines are CC's.
> Statuses: MET · MET–INCIDENT OPEN · IN PROGRESS · AT RISK · PENDING · HELD/PARKED.
> Last updated: 2026-07-18 18:05 IST · dev 1f3111b · gate-2 drives DONE (raw: 0 fail / 0 timeout — cleanest ever), judge running · local batch (PDF export fix-4e/f/g, wakeup2, whitelist ledger, VT-685 kit) pushes on green.

## O1 · Trustworthy conversation — MET (holding)
- Bar: Tier-1 trust-breakers = 0 across the 10-journey pack ×3; Tier-2 quality ≥ 90%.
- Now: met HEAD-authoritative (Tier-1=0, Tier-2=100%); every brain-touching change re-gates
  against the full pack before trusting.
- Gate: 651cb75 full-pack ×3 PASSED 2026-07-18 (30/30 scored; Tier-2 100%; one j05 sampled
  variance disambiguated 3/3-clean on re-drive → VT-684 rostered for the class). Graduates the
  VT-681 promise seam + the SR/turn-brain cache restructure. Next re-gate: O4 flags-on (gate-2).

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
- Gate to MET: canary-1 r1–r3 all FAILED on the media leg — root FOUND (Fazal screenshot +
  ledger evidence): the Twilio WhatsApp channel delivers PDF as its only document type; csv and
  plain-text both die async at Meta AFTER a successful create. **Fix-4g: list now renders as a
  PDF** (same weasyprint path as the monthly report) + fix-4f status-callback wiring (NO owner
  send on dev has EVER received a delivery callback — ledger was write-only) + fix-4e acks never
  assert visibility on a mere accept. LOCAL, rides the post-gate-2 push → **canary r4 = the PDF
  attempt**. Fazal console action: copy TEAM_TWILIO_WEBHOOK_URL (Vercel) → Railway dev
  TEAM_TWILIO_STATUS_CALLBACK_URL (value carries the bypass token — his hands, not CC's).

## O3 · Agent Capability Framework — MET ON DEV
- Bar: ratified Manager/SubAgent/Tool architecture live — SR + Onboarding on the contract,
  Integration dissolved into Tools, catalog + sufficiency enforced, no un-gated effects.
- Now: VT-101 migration complete + delta-gated on dev; 74-surface catalog; all 4 capability
  gaps closed; flags ON dev.
- Remaining: §7.3 DB-inversion (Fazal-explicit LAST) · prod flag promotion (rides VT-231).

## O4 · Autonomous Manager (management mandate §7) — IN PROGRESS
- Bar: plans (monthly/daily), allocates to specialists, validates outcomes, logs every
  decision with reasons — a manager, not a responder.
- Now: **VT-679 (§7A proactive planning) + VT-680 (§7C impact judge) BUILT + DEPLOYED on dev,
  flags ON**. Gate-2 ×3 drives COMPLETE — raw read is the cleanest full pack ever recorded
  (30/30, 69 steps passed, ZERO failures, ZERO timeouts); judge verdict pending (~15 min).
  Green closes both §7 named gaps. Reactive core + delegation + validation + audit live.
- Missing: proactive planning loop (§7A) mid-build depth · §7C impact-judge (validate outcome
  quality, not just completion) · dynamic sensing is O9 (held).

## O5 · Owner's language, owner's register — IN PROGRESS (CC-side DONE)
- Bar: every reply and agent-initiated message in the owner's language/register (en/hinglish/hi);
  mirroring wins live turns; preference governs agent-initiated.
- Now: VT-677 CLOSED — all phases landed + ×3 full-journey gate CLEAN (33 runs, hinglish
  journey 3/3, d9a4e10). D1–D3 built as ruled: hi-Latn register, live mirroring never
  overridden, no onboarding question, EN template fallback until Meta approves.
- Gate (Fazal/Meta-side): welcome hing SID registered (Meta approval pending) · wake-up v1
  (all 3 langs) FORCE-CONVERTED UTILITY→MARKETING by Meta (welcome2/3 class) → **team_wakeup2
  v2 registered** (account-fact copy, 3 SIDs) — awaiting Meta's category verdict. NOTE: the
  2026-07-18 whitelist ruling shrank O5's template scope to welcome+wakeup only (all other
  owner comms ride the 24h session — VT-683).

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
- Gate to lift AT RISK: canary r4 (the PDF attempt — O2 leg; r1–r3 taught: transport-level
  classes need the REAL phone, mocks can't see Meta's media policy) + a dirty slice folded into
  the standing ×3 cadence (Cowork roster call pending).

## O7 · Launch readiness (prod) — PENDING
- Bar: prod Mumbai live (VT-231), framework flags promoted, billing ₹5000/agent + per-agent
  trials, Meta template set approved, prod failed-workflow ops (VT-634 — VT-668 is its dev
  seed), signup exposure gate, ownership VTR gate.
- Now: dev-complete substrate; prod not started by design. Waits on Fazal calling VT-231.

## O8 · Learning loop & moat (Track C) — PARKED (post-trust-floor)
- Bar: the Manager learns per-tenant from the audit log; KG/RAG wired; per-capability
  accuracy graduation; concierge as the learning engine.
- Now: substrate exists (audit/trace, L2/L3, KG separate); RAG broker landed but UNWIRED;
  learning loop not started. Park condition (trust floor) is MET — awaiting Fazal's explicit
  un-park word.

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
  Accounting (prepare-only) · Tech (owner-authorised only) · Cost-Opt (advisory) ·
  **Compliance (NEW, Fazal 2026-07-18): Codex builds GSTR-1/3B filing-READINESS as a framework
  module (advisory/prepare-only; filing = declared-disabled until graduation; MCA parked —
  owner-docs only). VT-685 onboarding kit building now.**
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
