# VIABE TEAM — OBJECTIVES (status of record)

> Objective-level view — what we are trying to achieve and where each stands. NOT a task list
> (tasks live in the sprint dashboard / VT rows). **Maintainer: CC** (Fazal ruling 2026-07-18
> ~04:15 IST via Cowork 224500Z — this file IS the target checklist; CC updates it at every
> status-changing event, same moment as the to-cowork signal). Cowork audits (Rule #14).
> Bar lines are Fazal's — never edited without a ruling; Now/Gate/status lines are CC's.
> Statuses: MET · MET–INCIDENT OPEN · IN PROGRESS · AT RISK · PENDING · HELD/PARKED.
> **STALENESS RULE (Clau, 2026-08-05):** an objective whose `Now` line is >7 days older than the
> newest CL/VT touching it is STALE and must be reconciled BEFORE any status claim is made from
> this file. The scoreboard exists so Fazal can see the war in one page; a stale scoreboard is
> worse than none — it launders drift as status.
> Last updated: 2026-08-05 (**single-voice program complete on dev**: S2 choke PROD-LIVE, S3 ledger + S3b enforce-parity + S4 route-unification DEV-PROVEN, 3 of 4 casebook classes 3/3 in the completed pack (the 4th's target defect is fixed; its scenario is blocked by the SR/load timeout class, NOT rounded up). First trustworthy FULL PACK: 79/79 distinct scenarios, 237 entries, zero duplicates, zero contaminated. O8 un-parked and REACHABLE for the first time — migration 189 on dev, 15-card seed through the VT-710 pipeline, retrieval canary PASS. O11 sealed set independently authored + validated. OPEN: the SR approval/send lane. Re-drive at 150s COMPLETE — 6 of 9 gates now OBSERVED clean (a timeout is not a pass; these are real gate holds). **Root cause NOT established:** I proposed a 90s harness deadline sitting under the product's ~96s in-turn wait, then retracted it — measured latency shows NO successful turn above 90s (max 62.9s, approval-ask p90 61.3s), so the extra headroom was never used. The variable that actually changed was LOAD (79 back-to-back scenarios vs one in isolation). Residue: `sr_consequential_bulk_send_requires_approval` 0/3 with NO owner-visible reply — a real defect, reproducible in isolation, and the bulk always-confirm floor stays UNVERIFIED. Next: finish the re-drive → sealed no-O8 baseline → promotion on Fazal's word → VT-730.)

## O0 · NORTH STAR — Claude Code for Business — IN PROGRESS (the objective all others compose into)
- Bar (Fazal, ratified 2026-07-01 — Track C first-class): an owner hands the Team-Manager a
  business GOAL in one WhatsApp message, the way a developer hands Claude Code a coding goal —
  and the TM understands it in full context (never re-asks a known fact), plans the work,
  delegates to specialists and tools, executes end-to-end, validates its own outcomes, learns
  from every run, and stops ONLY at the deterministic effect gates (money/consent/approval).
  Autonomy is earned per capability from measured clean outcomes, never assumed.
- Composition (how the other objectives add up to this): O1 trust floor (MET) + O2 money
  integrity (MET, one named hardening in flight) + O3 the framework that makes capabilities
  pluggable (MET on dev) + O4 plan-delegate-validate (MET on dev, planning depth now real) +
  O5 owner's language (near) + O6 real-world reliability (at risk) + O8 learning loop
  (UN-PARKED, engine reachable) + O9 sensing (held) + O10 launch roster (in progress) + O11
  judgment measured (IN PROGRESS — harness + sealed set built).
- **Single voice (2026-08-05, new O0 property):** the Manager no longer contradicts itself
  durably. One emission choke (S2, prod-live), a ledger of what it has already asserted (S3),
  enforce-path parity (S3b), and gates that CLASSIFY while only the composer SPEAKS (S4). THREE of
  the four measured self-contradiction classes are dead 3/3 in the completed full pack; the
  fourth's target defect is fixed but its scenario still blocks under load, so it is NOT counted. "Speaks with one voice" moved
  from an aspiration to a property with a test behind it.
- Honest stage: **a trustworthy reactive-plus-planned OPERATOR is real on dev today.** The
  gap to the full north star is exactly O8 + O9 + soak-proven depth: it does not yet LEARN
  from its runs, does not yet SENSE and initiate without a trigger, and its planning depth
  is young. Those are sequenced behind the trust floor deliberately — a Claude Code that
  can't be trusted isn't one.
- Bar: Tier-1 trust-breakers = 0 across the 10-journey pack ×3; Tier-2 quality ≥ 90%.
- Now: met HEAD-authoritative (Tier-1=0, Tier-2=100%); every brain-touching change re-gates
  against the full pack before trusting.
- Gate: 651cb75 full-pack ×3 PASSED 2026-07-18 (30/30 scored; Tier-2 100%; one j05 sampled
  variance disambiguated 3/3-clean on re-drive → VT-684 rostered for the class). Graduates the
  VT-681 promise seam + the SR/turn-brain cache restructure. Next re-gate: O4 flags-on (gate-2).

## O2 · Money-path integrity — MET, RE-PROVEN after an incident (VT-734)
- **2026-08-06 incident + re-proof (the bar's own words: "can never perform OR CLAIM a money action
  falsely" — the PERFORM half was violated, and it is the half that was re-proven).** On deployed
  dev an owner sent the same campaign request twice while the turn was slow; the SECOND REQUEST —
  sent **72 seconds BEFORE the campaign_send approval existed** — resolved it `approved`, and 19
  customers were really messaged (mocked at the Twilio client only because the harness number is
  bogus; 19 `campaign_messages` rows, `campaigns.status='sent'`). Two holes composed: the VT-633 D-A
  arm-wait lets an inbound reach FORWARD to an approval armed after it, and `bhej` sits in
  `_APPROVE_VERB`, so the request text itself classified as consent.
  **NOT a fabrication:** I first reported "claimed a send with zero campaign_messages" as a third
  defect and RETRACTED it — the rows exist; my query joined a column the send path never writes. The
  manager told the truth, which makes this worse rather than better.
  **Fixed to Fazal's ruling (CL-2026-08-06-repeated-request-is-never-approval), both halves:** an
  ordering invariant at the single resolution choke point (the resolving inbound must be strictly
  newer than `COALESCE(presented_at, requested_at)`; mig 190 records presentation at delivery; fails
  CLOSED) and a content rule ahead of every classifier (a reply adding nothing to the original
  request is a re-ask, never consent; "haan bhej do" still approves).
  **Re-proof ×3 on deployed dev:** approval `pending`/no decision, campaign `proposed`, **0 messages
  sent** — three runs of the exact breach shape, versus `approved`/`sent`/**19** before.
- Bar: the Manager can never perform OR claim a money action falsely — DB is sole authority,
  stated values bind to DB, approvals never resolve into silence, corrections revise.
- Now: proven in code/unit/DB asserts (CL-2026-07-16, VT-667/668/670). VT-671 wake-on-signal
  landed + ×3-proven (a57514b): approval resolutions wake the waiting workflow instantly —
  "approved into silence" latency tail dead; first-ever full j01 pass.
- INCIDENT (narrowed, one leg left): diagnosis DONE 2026-07-18. Canary-2 (VT-668 re-arm leg)
  was NOT a breach — conversation_log proved Fazal's own phone approval 13:49 + honest outcome
  report 15:10; live proof PASSED. Canary-1 (customer-list) root-caused → F1–F3 landed 42bd7e6
  (plain-ask delivers, guard copy time-grounded, export_customer_list manager tool).
- INCIDENT CLOSED 2026-07-18: canary r4 PASSED (Fazal-confirmed PDF delivery). The three-run
  root-cause: Twilio WhatsApp delivers PDF as its only document type — list now renders via the
  monthly-report weasyprint path. Delivery-status callback wired (TEAM_TWILIO_STATUS_CALLBACK_URL
  set by Fazal); watch: first 'delivered' flip pending. VT-676 CLOSED.
- NEW LEG PROVEN (2026-08-03): the VT-719 asserted-facts ledger + VT-722 enforce-parity mean a
  stated commitment is recorded on EVERY mode path, not just the walker's — a commitment can no
  longer be silently contradicted because the mode that made it didn't write it down.
- OPEN FINDING (O2-class, structural fix rostered): the **ask/arm race** — an owner's decision can
  land before the approval row is armed, with a hand-rolled ~96s poll loop acting as referee.
  Refereeing a race is not the same as not having one. **VT-730** is the fix: an arm-before-ask
  INVARIANT enforced at the VT-718 choke (an approval-ask is unemittable unless its id references
  an armed row) plus a durable DBOS mailbox replacing both poll loops. Sequenced AFTER the
  promotion — it is money-path surgery and does not get rushed into a promotion window.

## O3 · Agent Capability Framework — MET ON DEV
- Bar: ratified Manager/SubAgent/Tool architecture live — SR + Onboarding on the contract,
  Integration dissolved into Tools, catalog + sufficiency enforced, no un-gated effects.
- Now: VT-101 migration complete + delta-gated on dev; 74-surface catalog; all 4 capability
  gaps closed; flags ON dev.
- Remaining: §7.3 DB-inversion (Fazal-explicit LAST) · prod flag promotion (rides VT-231).

## O4 · Autonomous Manager (management mandate §7) — MET ON DEV (gate-2 passed; planning depth now REAL)
- Bar: plans (monthly/daily), allocates to specialists, validates outcomes, logs every
  decision with reasons — a manager, not a responder.
- Now: **§7A + §7C CLOSED — gate-2 PASSED** (1f3111b, flags ON): cleanest full pack ever
  (30/30, 0 step failures, 0 timeouts); judge Tier-2 96.6% PASS; the single judged Tier-1 was a
  judge-FP deterministically refuted by the DB money-authority asserts (stated 8 == DB 8 —
  CL-2026-07-16: DB is sole authority; transcript-artifact misread, VT-641 family). Proactive
  planning + impact judge are LIVE on dev.
- Remaining: depth/robustness measured over the soak (phase1-plan C-track) · dynamic sensing
  is O9 (held). *(Cowork audit-patch 2026-07-18: header + this line reconciled to the gate-2
  Now — the old "Missing" line contradicted it. CC: verify on next write.)*

## O5 · Owner's language, owner's register — IN PROGRESS (CC-side DONE; blocked only on Fazal/Meta)
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
- **CADENCE CALL MADE (Clau 2026-08-05, after sitting unmade since July):** the `--dirty` seed
  slice folds into the STANDING full-pack cadence, **alternating clean/dirty per ×3 cycle** — not
  doubling the pack, because a ~10-hour serial run is fragile enough already (two were lost to
  environmental faults on 2026-08-03/04). First dirty cycle = the first standing pack AFTER the
  promotion. **When one dirty cycle completes with no new failure class, O6 lifts AT RISK** per its
  own bar. An unmade roster call is how an objective gets overlooked while looking tracked.
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
- Gate to lift AT RISK: canary leg CLEARED (r4 PASSED 2026-07-18). Remaining: dirty slice
  folded into the standing ×3 cadence (Cowork roster call pending) → then AT RISK lifts.

## O7 · Launch readiness (prod) — PENDING
- Bar: prod Mumbai live (VT-231), framework flags promoted, billing ₹5000/agent + per-agent
  trials, Meta template set approved, prod failed-workflow ops (VT-634 — VT-668 is its dev
  seed), signup exposure gate, ownership VTR gate.
- Now (ground truth 2026-08-05, verified against prod by read-only audit): **#547 PROMOTED —
  main at `62a8b595`.** Prod Mumbai is live; prod migrations applied through **187**; all twelve
  O8 tables EXIST on prod and every one is EMPTY. **Gap to the next promotion is exactly two
  migrations: 188 (VT-721 week plans) + 189 (VT-726 card projection)**, both additive.
- Prod-migration pre-check CLOSED by measurement, not assumption: 189's `ADD COLUMN domain TEXT
  NOT NULL` has no DEFAULT and would FAIL against a non-empty `knowledge_cards`. Prod's registry
  was verified empty, so it is safe. Prod's `app_environment` sentinel reads `prod`, so
  `--expected-env prod` will match when Fazal authorises the run. No prod migration has been run —
  that is Fazal's word (CL-431).
- Gate: the promotion is PRE-AUTHORIZED by Fazal on completion of the queue but still opens on his
  explicit word. Blocking item: the SR approval/send gates must report OBSERVED passes — a timeout
  is not a pass, and those nine are the Pillar-7 money/consent floors.

## O8 · Learning loop & moat (Track C) — IN PROGRESS (LAUNCH-CRITICAL, un-parked Fazal 2026-07-29)
- Bar: the Manager learns per-tenant from the audit log; KG/RAG wired; per-capability
  accuracy graduation; concierge as the learning engine.
- Now (2026-08-05): un-parked by Fazal 2026-07-29 and **reachable for the first time**. Engine
  merged (`card_retrieval.py`); **migration 189 applied on dev**; a **15-card seed admitted THROUGH
  the VT-710 pipeline** (a hand-authored INSERT is refused in code, not just in prose); **retrieval
  canary PASS — the first card the engine has ever returned** (`result_count: 1`, considered 15,
  global purity pass, `authorizes_effects: false`), and I traced the embedding path to confirm the
  candidates came from the real provider and not a fallback.
- VT-725 consumer BUILT, shadow-only and unwired: shadow-safety is structural — the returned type
  exposes no field, method or property carrying card text, so "shadow accidentally injected" is not
  a bug that can be written. Flip/narrowing canary (exit gates b + d) in flight.
- VT-726 DONE. VT-727 scoped against MEASURED disposition: 118 card records from **88 files** (not
  118 files — corrected), 64 immediately shadow-admissible, 54 deferred, 0 rejected. Two review
  GATES before ingestion: independence clustering currently follows SOURCE IDENTITY not semantics
  (so corroboration count is not evidence of independent confirmation, and a T4 card could be
  promoted by two retellings of one study), and 3 of 5 T1v cards are Reddit threads (tier drives
  CONFLICT RESOLUTION, so a mis-tiered card can win an adjudication against a correct one).

## O9 · Dynamic sensing (Phase 1.2) — HELD (by design)
- Bar: watchers/pollers/listeners the Manager configures; event-driven autonomy.
- Now: spec written + held from CC until Phase 1.1's gate is met (Fazal's sequencing).
- **DELIBERATE, not forgotten.** O9 is the ONE objective with no work in flight and that is
  correct — Fazal's explicit sequencing. Recorded here so "held" is visibly different from
  "overlooked" (the orphan check below cuts both ways).

---

## Objective ↔ in-flight map (every line of work must trace to an objective)

| In flight | Objective it serves |
|---|---|
| SR re-drive @150s (COMPLETE — 6/9 gates observed clean) | O0/O2 — send-gate floors, observed not assumed |
| ~~`sr_consequential_bulk_send_requires_approval` silent-turn defect~~ RESOLVED into three causes | **O2** — the bulk always-confirm floor is **VERIFIED**: draft + explicit approval ask + 0 sent, observed repeatedly |
| No-O8 baseline (next dev-queue stage) | **O11** — and it unblocks D3 + D7 |
| VT-725 flip/narrowing canary | **O8** — serving, shadow |
| VT-727 full ingestion (Codex, after canary) | **O8** — the moat's corpus |
| VT-723 (Codex, after baseline) | O8 × O11 — coverage aimed at measured weak slices |
| dev→main promotion (Fazal's word, after pack) | **O7** |
| VT-634 containment design → build | **O7** bar item (7 decisions pending with Fazal) |
| VT-730 ask-readiness + mailbox + liveness | **O2 hardening + O0 reliability** (post-promotion) |
| Hardcoded-Sonnet call sites bypassing `resolve_chat_model` | O7 cost/ops — Fazal's burn question |
| D1/D2 Meta console | **O5 + O10** (Fazal-side; the only human-gated items) |
| O6 dirty-cadence (alternating, post-promotion) | **O6** — lifts AT RISK on one clean dirty cycle |

**Orphan check, both directions:** work with no objective ⇒ stop and ask why we are doing it.
An objective with no work AND no owner ⇒ flag same-day. Currently every objective is covered
EXCEPT **O9** (held by Fazal's sequencing) and **O11's C4 graduation linkage** (lands with
O8/D7) — both deliberate, both stated rather than silent.

## O11 · Business judgment quality — measured, not asserted — IN PROGRESS (baseline is the next dev-queue stage)
- Bar (phase1-plan Track D, reshaped 2026-07-01): the quality of the Manager's business
  DECISIONS and advice is SCORED, not assumed — the advice-quality eval (factuality /
  actionability / relevance / tone) runs on a held-out measurement set, and per-capability
  autonomy graduation (C4) is gated on measured clean outcomes, never elapsed time. No
  fabricated numbers/benchmarks (the surviving claim-grounding rail). "Understands business"
  becomes a provable claim.
- Now (2026-08-05): the rail (no-fabricated-figures) is live via the trust floor. **The eval IS
  built**: VT-705 harness + the response-bundle generator (which did not exist — the harness could
  validate datasets and score bundles, but nothing produced one, so the baseline was unrunnable).
  The sealed held-out set was authored INDEPENDENTLY of both Clau and CC and **validates PASS** (12
  cases, digest `257c9674…`, family isolation proven against both visible partitions). Custody is
  structural: the generator reads a case's agent-view only and has no verbose flag. The frozen
  no-O8 baseline is the next dev-queue stage; graduation linkage (C4) still not built. Distinct from Tier-2 (conversation quality) and
  §7C (outcome-vs-defined-outcome): this scores whether the decision was GOOD.
- Sequencing: the measurement harness can start BEFORE O8 un-parks (it measures today's
  LLM+data judgment; the learning loop then has a baseline to improve against). Graduation
  linkage lands with O8/C4.

## O10 · Phase-1 launch roster — agents + tools READY — IN PROGRESS
- **VT-681 registry gate CLOSED (2026-08-05):** the line read "×3 re-drive pending"; gate-2 passed
  2026-07-18 and the capability-truth seam has since ridden every full pack, including the first
  complete 79/79 run (2026-08-05) with zero capability-promise failures. Nothing genuinely left on
  that gate. Remaining O10 work is D1/D2 Meta-side, which is Fazal's, not CC's.
- Bar (phase1-plan LOCKED, "Function scope at launch" + ACF §5): every launch agent, function
  mode, and tool live and correctly labelled at Concierge launch — the Manager may promise
  ONLY capabilities marked live for that tenant/environment.
- Agents: **Manager** (embedded) LIVE · **Sales Recovery** LIVE (Concierge; first eligible to
  graduate) · **Onboarding Conductor** LIVE. Advisory functions as Manager-held tools, never
  described autonomous: Marketing (prepare+propose via send rails) · Finance (advisory) ·
  Accounting (prepare-only) · Tech (owner-authorised only) · Cost-Opt (advisory) ·
  **Compliance (NEW, Fazal 2026-07-18): Codex CLEARED to build GSTR-1/3B filing-READINESS as a
  framework module** (advisory/prepare-only; filing declared-disabled; MCA parked — owner-docs
  only). VT-685 kit SHIPPED + hardened through THREE Codex review passes
  (EXTERNAL-BUILDER-ONBOARDING.md, engagement-agnostic; 9-check conformance documented;
  wrappers-first DB rule; wire-to-live CC-owned; spawn_integration annotated LEGACY in the
  generated catalog).
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
