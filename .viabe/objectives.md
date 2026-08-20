# VIABE TEAM — OBJECTIVES (status of record)

> ## ⚡ O12 · OPERATION FIRST PROOF — THE BINDING OBJECTIVE (Fazal, 2026-08-18) — IN PROGRESS
> **Bar (Fazal's, verbatim intent):** the launch statement *"Our own AI team does our Sales and
> Marketing; here are the numbers"* — RKEcom is TENANT ZERO on prod; Viabe Team runs the sales &
> marketing of Viabe Reports (Location Feasibility live · Market Intelligence upcoming) through a
> **two-week proof window** producing attributed, err-under numbers.
> **Full specification (PINNED): `.viabe/FIRST-PROOF.md`** — six agents (Acquisition, Lead
> win-back, Campaigns, Checkout rescue, Content writer, Ad planner), P0–P3 tracks, red lines,
> tenant-zero pre-registration. **Tenant workflow diagram (PINNED):
> `docs/diagrams/tenant-workflow.svg`** — the tenant-facing picture of what the team does.
> **Composition into the ladder:** O12 is O0's first real-world instantiation. It SUPERSEDES the
> M4–M7 milestone shape (M1 ✅ M2 ✅ M3 → P0.1); every other objective below keeps its bar and now
> serves O12: O2/O6 (rails under real money) · O5 (owner language live) · O7 (promotion = P0.3) ·
> O8 (shadow serving live; capture ledger = the proof's evidence) · O11 (the value compiler's
> first real run). **Dates flex, sequence binds** (Fazal 2026-08-18).
> **Red lines (constitutional):** no autonomous ad SPEND · no send without consent basis — our own
> gates bind RKEcom hardest of all.

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
> Last updated: **2026-08-19, CC (M2 CLOSED · VT-725 gate (a) CLOSED on deployed evidence; gate (f) open)** — CURRENT STATE:
> **M2 closed on eleven rows and a
> measured pack number.** Full pack ×3 on the post-M2 build: **353/390 = 90.5% clean** vs gate (d)'s
> 338/390 = 86.7%, **0 TIMEOUT and 0 INDETERMINATE across 870 steps**, all domain floors clean;
> stable-fails 10 → 4. **integration 100%** (96.2), **onboarding 100%** (89.7), manager 86.8% (79.8),
> sr_autonomy_rails 80.4% (85.3 — a regression, found and fixed, see below). All six targeted rows
> landed CCC. With the two in-batch regressions verified fixed the projection is 362/390 = 92.8%, but
> the MEASURED number is 90.5% and that is the one to quote.
> **Built:** VT-758 (a no-send safety assert now QUERIES instead of defaulting to zero — 11 scenarios
> declared it, 8 unguarded) · VT-759 (enforce mode had no DEFER beat and no PROFILE-ACK beat; **the
> row's own diagnosis was wrong** — of 33 English-literal asserts exactly ONE was an instrument error)
> · VT-756 (a count is honest only when its source is known present; two more of the shape found,
> including an empty CSV announced as "I've sent your customer list") · VT-761 (a connect request
> written in the participle; the mis-route stranded the NEXT turn too — reverses a deliberate
> 2026-07-10 trade, stated in the row) · VT-752 item 1 (stage boundaries across the run handoff; the
> row asked for `duration_ms` on `pipeline_steps`, which structurally cannot span runs) · VT-757 (the
> capability check now runs before the ack — and then had to run before a KEYWORD net that was
> overruling it) · VT-755 scopes 0+1 (D-A: `ask()` delivers through the single choke, the park is
> conditional on delivery, raw model remediation never reaches an owner) · VT-754 (D-C: attribution
> requires an attributable signal; scope 4 was a SECOND independent zero — nothing ever armed
> `attribution_close_at`) · VT-745 (D-B part 2 verified already-satisfied and pinned; part 1 NOT built
> — no production surface composes a customer-facing link, so a mint caller would be the sixth
> built-and-called-by-nothing this fortnight; Fazal's call which surface earns it) · **VT-762**
> (claude-sonnet-5 thinks by default and the thinking counts against `max_tokens`: triage at 200
> returned a thinking block and no text, 8/24 → 0/24 after the floor) · **VT-763** (the SR evidence
> enum named three sources the `tools=[]` specialist cannot always have and NONE for the context
> bundle it always has, so the model refused to plan on a cohort it could see — **verified 18/18 on
> dev** against a 3/8 pre-fix baseline; the corrective retry fired twice in those 18 and recovered
> both, so the deterministic net carries it, not the prompt).
> **Open, mechanisms named:** VT-764 (a token guard that trips every run and is swallowed by the
> callback manager — a guard that cannot fire), 4 stable-fails (down from 10), VT-755 scope 2,
> VT-752 items 2–5, VT-754 scope 5, VT-745 part 1.
> **M3 IN PROGRESS (2026-08-17): VT-742 §1 BUILT + PROVEN ON DEV.** `resolve_sender(tenant_id)` is
> now the only source of a WhatsApp sending number (own live WABA > pinned shared > default shared,
> fail-closed), all three `messages.create` sites consume it, migration 207 applied to dev, and a CI
> gate makes the old env read un-reintroducible. The row was written as reputation hygiene; the
> functional half is bigger — customer inbound resolves the tenant by the number the customer
> messaged TO, so under the shared sender **a customer could be messaged but not heard.** A customer
> send therefore now REFUSES rather than downgrading to shared. 82/82 green on deployed dev across
> the sender, transport, dev-guard and WABA suites; RED-proven by restoring the env read (3 tests
> fail + the gate fails on the exact line). Exit gates (a)(b)(c)(g) CLOSED; (d)(e)(f) belong to
> scopes 3–6, not built. **Four pre-existing defects uncovered:** the harness wrote a non-E.164 WABA
> number for months (`+1555<uuid-hex>` — also the one malformed live number of 134 on dev); three
> tests could only pass ONCE against a given database (one of them presented as a pooler error and
> was a `UniqueViolation`); `tenant_whatsapp_accounts.phone_number` had no uniqueness while the
> inbound lookup reads it with `LIMIT 1`; and `wa_send_allowed` passes a `live` row with a NULL
> number.
> **VT-725 scope 1 + 5 WIRED, DELIBERATELY DARK.** `retrieve_cards_for_turn` has worked since VT-723
> with ZERO callers in `src/` — twelve tables, 118 eligible cards, and the Manager could not read one
> of them. It now has callers at the Manager's framing-context assembly and at specialist dispatch,
> through ONE seam, with an injection tripwire (the seam asserts `INJECTS_INTO_PROMPT is False` before
> serving), an undeclared-identity refusal, replay-stable attribution keys, and 13 tests.
> `TEAM_KNOWLEDGE_SERVING` stays UNSET: flipping it would write `decision_evidence_links` while 63 of
> 100 eligible cards scope nothing, manufacturing a causality baseline we would then have to distrust.
> **Gates (a)-(f) remain OPEN** — blocked on VT-749 scope 1 (Clau, Aug 19); the flip is then one
> variable, not a code change. One test in that batch passed with the serving gate FORCED OPEN (the
> fail-soft `except` swallowed the tripwire's own AssertionError) — found by forcing it, rewritten to
> record calls rather than raise.
> **2026-08-18 — VT-749 LANDED AND VT-725 SHADOW SERVING IS LIVE ON DEV.** Clau delivered the scoping
> two days early; it is applied through the plan builder AND to the served corpus (migration 208 →
> corpus v4): **63 unscoped-and-eligible cards → 0**, with 42 now declaring `universal` deliberately,
> and the gate-(a) pin INVERTED so an unscoped card can never be added silently. A landing trap was
> caught first: the delta's `card_version_id`s are not the rows serving reads (0 of 63 are members of
> the served corpus), so keying on them would have reported "63 scoped" and changed nothing — resolved
> by `card_key` against the served members instead. **VT-725 gates (a)-(e) CLOSED, canary PASS**:
> manager 100 candidates / 8 selected, specialist 37 with ZERO out-of-lane against 102 decoys,
> `decision_evidence_links` 200 rows, and both forced failure depths degraded rather than raised — so
> the per-tenant flip Fazal ratified demonstrably works at runtime with no deploy. Gate (f) (full pack)
> is running. Nothing is injected: shadow cannot reach a prompt, and the seam refuses to serve if that
> ever changes.
>
> **2026-08-19 — CORRECTION, and it is a big one: shadow serving was never actually live.** The line
> above is true about the flag and false about the behaviour. Two things were wrong at once and each
> alone was sufficient. First, the deployed dev service had **no `VOYAGE_API_KEY`** — dev logs show a
> real `manager_turn:` degrading to no-cards with `EmbeddingKeyMissingError` and not one non-degraded
> serving line. Second, and worse because the key fix did not cure it: the retrieval call lived inside
> **`dispatch_brain`, which is exactly the router `skip_legacy_dispatch` skips** — and dev runs
> **enforce** mode, where the triage seam owns `new_task`/`answer_pending` turns and `dispatch_brain`
> never runs. So the Manager's most common turns on dev consulted the corpus **never**:
> `decision_evidence_links` held **zero** `manager_turn:` rows across the entire life of the feature.
>
> The canary that closed gate (a) calls `retrieve_cards_for_turn` **directly**, and the wiring test
> greps `dispatch_brain`'s source — so both passed on a path no live turn takes, and the wiring test's
> own docstring had said it could not prove a live trace. **Gates (a) and (f) are REOPENED.** Fixed:
> key set on dev, retrieval moved to the per-turn path in `runner` ahead of the branch, pin moved with
> it. Also fixed in the same pass: **migration 208 raised on any fresh database** (the corpus is seeded
> by `registry_seed`, not by migrations, so it resolved 0 of 63 and could not tell "nothing to do" from
> "the corpus drifted") — which is why the pre-push suite was red and nothing had been pushed since
> 04:06Z. The lesson worth keeping past this row: **three separate failures tonight were a missing
> environment variable wearing a defect's clothes**, and the fourth was a green test on an unused path. Also in the window: **VT-764** (the swallowed token guard — output-token axis sized from
>
> **2026-08-19 (later) — gate (a) is CLOSED, and one half of the entry above is WITHDRAWN.** A real
> deployed turn wrote **200 `decision_evidence_links` rows** (`manager_turn:SMharness28f2d930…`,
> 100 retrieved / 100 rejected), on a build whose image digest is verifiably distinct from the
> pre-fix one. **The correction:** I reported TWO causes; there was ONE. The claim that the seam sat
> behind `dispatch_brain` and so "the corpus was consulted never" rested on zero evidence rows —
> and those zeros were the HARNESS deleting its throwaway tenant at teardown, cascading the rows
> away before I queried. The identical scenario re-run with `--keep-tenants` produced the 200 rows.
> The 04:05Z degrade line, which carried a `manager_turn:` decision id under a build whose only
> caller was `dispatch_brain`, actively contradicts the skip theory. **What stands is the single
> real cause: the deployed service had no `VOYAGE_API_KEY`,** so real turns reached the seam and
> fail-softed to no-cards; the canary passed throughout because it ran locally with a key the
> deployment lacked. The runner move is kept on a narrower justification (a gate-relevant seam
> behind one of two routers is fragile), not as a fix for an observed miss. **The instrument lesson
> outlives both:** a harness that deletes its tenant destroys the evidence a shadow feature exists
> to produce, so the proof is guaranteed absent and reads exactly like failure.
> a 7-day ledger query, `raise_error` on the callback) plus a correction to my own overclaim about it,
> **VT-765** (a customer's reply to the shared number resolves to NO tenant — Critical, Fazal owns the
> routing choice), **VT-766** (74 aborts, zero recorded reasons), and a sanctioned env SETTER closing
> Rule #18's write-direction gap. Regression pack on the pre-shadow build: **128/130 = 98.5% clean,
> 0 TIMEOUT, 0 INDETERMINATE, all floors clean** (single pass — not comparable to M2's run-level 90.5%).
> Prior header
> (2026-08-06, superseded): **VT-734 — CURRENT STATE: **VT-734
> approval-breach FIXED + DEV-PROVEN** (mig 190; ordering invariant + repeat-refusal; breach repro
> ×3 → pending/proposed/0-sent; the Manager's send claim was TRUE — CC's "fabricated claim" was its
> own broken verification query, retracted). **Critical ×3 consistency gate IN FLIGHT on the full
> fixed build (VT-732 model governance + bulk-send + VT-734): 34/79 distinct at last read, ALL
> clean, zero blocks/divergence/contamination** — cleanest run ever recorded. Wedge deferred BY
> FAZAL into this gate (call-out either way). Next, per Fazal "close RAG today": sealed no-O8
> baseline (O11) → VT-725 flip/narrowing canary (O8 serving evidence) → promotion package →
> Fazal's word. **Commercial bar added: the PILOT PROOF GATE P1–P6** (launch-tracker,
> CL-2026-08-06-pilot-proof-gate) — P1 ≥60% wk-8 active · P2 ≥40% pay · P3 ≥50% at 3× value
> (attributed gross profit, VT-733-C) · P4 zero gate violations · P5 founder-free onboarding ·
> P6 autonomy grants rise. Pricing STRUCTURE ratified (Manager free · flat/specialist all-in ·
> per-agent free month + mandate); level waits on measured cost. Rolling queue:
> `.viabe/queue/ROLLING-QUEUE.md`. Prior header (2026-08-05, superseded): (**single-voice program complete on dev**: S2 choke PROD-LIVE, S3 ledger + S3b enforce-parity + S4 route-unification DEV-PROVEN, 3 of 4 casebook classes 3/3 in the completed pack (the 4th's target defect is fixed; its scenario is blocked by the SR/load timeout class, NOT rounded up). First trustworthy FULL PACK: 79/79 distinct scenarios, 237 entries, zero duplicates, zero contaminated. O8 un-parked and REACHABLE for the first time — migration 189 on dev, 15-card seed through the VT-710 pipeline, retrieval canary PASS. O11 sealed set independently authored + validated. OPEN: the SR approval/send lane. Re-drive at 150s COMPLETE — 6 of 9 gates now OBSERVED clean (a timeout is not a pass; these are real gate holds). **Root cause NOT established:** I proposed a 90s harness deadline sitting under the product's ~96s in-turn wait, then retracted it — measured latency shows NO successful turn above 90s (max 62.9s, approval-ask p90 61.3s), so the extra headroom was never used. The variable that actually changed was LOAD (79 back-to-back scenarios vs one in isolation). Residue: `sr_consequential_bulk_send_requires_approval` 0/3 with NO owner-visible reply — a real defect, reproducible in isolation, and the bulk always-confirm floor stays UNVERIFIED. Next: finish the re-drive → sealed no-O8 baseline → promotion on Fazal's word → VT-730.)

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
- Now (2026-08-14, CC): **STAYS AT RISK, and the reason is now named rather than statistical.** The
  SR pack's residue is not a spread of flakes — after VT-753 fixed the instrument, four of the five real
  failures are ONE defect (VT-755): a task parked on a question that was never sent, which then queues
  every later objective for that tenant behind it forever. That is a real-tenant reliability failure of
  the worst shape (silent, unrecoverable, unalarmed) and it is squarely this objective's bar. Three of
  its defects are closed and a `critical` wedge alert now exists, so the failure is no longer SILENT —
  but O6 does not lift on an alert, it lifts on the dirty cycle passing with no new failure class.
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
- **2026-08-07 — the corpus was RLS-INVISIBLE to the application, and that is now FIXED.** Eight O8
  tables carried `ENABLE ROW LEVEL SECURITY` with **zero policies** = deny-all to any non-owner role.
  `app_role` read **0** rows where `postgres` read 182, so the curated corpus had never been readable
  by the application on ANY environment — VT-725's "nothing calls the engine" was only half the gap.
  **Migration 196** adds SELECT-only policies `TO app_role` (writes stay closed so an agent can never
  author its own knowledge; `TO public` refused because Supabase's default grants would have exposed
  the corpus via PostgREST — REVOKEs added). Dev after apply: app_role sees 182 cards / 64
  retrieval-eligible / 118 embeddings.
- **VT-725 exit gate (d) specialist narrowing: PROVEN on dev.** Manager 64 candidates,
  `sales_recovery_agent` 25 — its lane only, with other-domain cards present so a leak had somewhere
  to appear. A positive proof, not an empty-result pass.
- **VT-725 exit gate (b) per-tenant flip: BLOCKED, and NOT on RLS.** No card can clear the Manager's
  `minimum_score = 0.62`: holding every component at its measured value and setting semantic
  similarity to a perfect 1.0 gives a ceiling of **0.5407**; best actually observed **0.270**. Two
  causes, both metadata/design rather than code — every card has `applicability_universal = false`
  with empty industries/size_bands/maturity_stages (max unknown-dimension penalty → applicability
  0.10), and `_recency()` returns **0.0 by construction** for evergreen cards (non-T1, no
  `effective_to`, no `expires_at`). **Open on Fazal:** which cards are genuinely universal is a claim
  about the knowledge, and moving the floor/weights is a design call; CC did not tune the gate.
- **O11 treatment arm did not exist.** `--knowledge-mode` was only RECORDED in the bundle and never
  reached the prompt, so a "treatment" run would have been the baseline wearing a label and its score
  pure sampling noise. Real retrieval built (`canaries/o11_knowledge.py`) with honesty
  instrumentation that states outright when an arm injected nothing. Sealed baseline DONE (12/12,
  real); sealed treatment deliberately NOT run while the floor blocks injection.
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
- Now (2026-08-14, CC): **the measurement itself was the blocker and is now fixed.** Every prior
  "delegation miss" number on this objective was produced by a harness that claimed a ROUTING verdict
  from a 150s clock against ~390s of real work (VT-753). It now settles to TERMINAL, and an unmeasured
  run reports INDETERMINATE — a bucket that must never be folded into a miss rate. Gate (c) re-drive on
  the fixed instrument: TIMEOUTs 4 -> 0, 2 of 7 scenarios were pure latency and are CLEAN, 5 real, 4 of
  those 5 one defect. Gate (d) (full pack x3) is running. **No rate should be quoted from this objective
  until VT-755 lands** — it would measure VT-755 and call it judgment.
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
