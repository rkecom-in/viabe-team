# Team-Manager Objective — the north-star CC optimizes toward

Owner: Claude Code (implementer). Authored 2026-07-09 on Fazal's ask ("define the objective CC is trying to
achieve"). This supersedes "chase the 45% gate number" as the target. The VT-611 conjunctive gate is a
THERMOMETER, not the objective — see §4.

## 1. The objective (one sentence)
**A small-business owner in India would trust the Team-Manager to run their business operations over WhatsApp
unsupervised** — i.e. it never does something that makes the owner fire it, and it is competent enough to be
worth keeping.

**"Run operations unsupervised" is two layers:** §1–§6 define the REACTIVE floor (respond to the owner without
breaking trust + competent conversation). §7 (Fazal 2026-07-10) defines the PROACTIVE-MANAGER mandate that
"run operations" concretely requires — **plan, lead, validate, audit** — which sits ON the trust floor.

## 2. Acceptance = TWO tiers (this is the real target, per Fazal 2026-07-08)

### Tier 1 — TRUST-BREAKERS = 0 (a COUNT, hard gate)
One occurrence and the owner loses trust; no average smooths it over. A trust-breaker is any of:
1. **Fabrication** — inventing a fact / number / price / business identity / capability not grounded in the
   owner's data or message (invented store name/city/type, made-up pricing, ungrounded ₹ figures).
2. **Wrong or dropped money action** — sending or failing to send a campaign/spend against the owner's actual
   instruction; arming/charging incorrectly; a delegated money task silently never executing.
3. **Loop / stall** — repeating a prior message, question, or link (verbatim OR semantic) with no new
   information; stalling on "I'm on it / I'll update you shortly" without ever delivering the result.
4. **Ignoring the speech-act** — not answering what the owner actually asked (a direct question gets a
   campaign; a correction gets a stall; a count/status ask gets a non-answer).
5. **Promising the impossible** — committing to something the platform cannot do (e.g. "I'll post to your
   Instagram" when it can't; a Zomato/Swiggy action it can't perform).
6. **Clearly-wrong action or tool** — took a business action / picked a specialist / proposed a next-step-set
   that is CLEARLY wrong for the situation when a correct one was obvious (routed a finance question to the
   sales lane; drafted+armed a campaign when the owner only asked a question; executed when it should have
   advised, or advised when it should have executed). BOUND: **clearly-wrong**, not merely-suboptimal — a
   defensible-but-not-optimal call is NOT a trust-breaker (it lands in the Tier-2 quality band). This is the
   decision-judgment breaker: a well-worded reply that makes the WRONG operational call still breaks trust.
**Target: 0 scenarios with any trust-breaker. Measured per-transcript, not from an average.**

### Tier 2 — QUALITY ACCEPTANCE ≥ 90%
Of the scenarios with NO trust-breaker, the fraction where the manager's handling is genuinely good — competent,
advancing, right tone + language. **Target ≥90% to ship as "trustworthy to run a business"; 95% = excellent.**
This is deliberately LOOSER than the conjunctive gate: an honest, correct, advancing reply that isn't a
straight-5 is still trustworthy.

## 3. Capability behaviors the objective requires (concrete — "quality" is not vague)
| Behavior | What it means | Measured by |
|---|---|---|
| **Context-aware / never re-ask** | Uses facts already given; never re-asks a stated fact | no re-ask of a fact present in the conversation/profile |
| **Advancing** | Every reply moves to the next concrete step | progression: not a restate/loop |
| **Multi-step execution** | A task yields a real plan/execution, not one canned step | delegated task returns a substantive plan/result |
| **Delegation-and-surfacing** | Delegated work's RESULT reaches the owner | no "I'm on it" → silence |
| **Honest / grounded** | No fabrication; honest "I don't have X" + a next step | honesty; capability-grounding |
| **In-register** | Mirrors the owner's language (Hinglish→Hinglish) | language match |
| **Decision correctness** | Given the situation + owner goal, chose a SOUND business decision/strategy | judged vs what a competent operator would do (needs a right-call ground truth) |
| **Right tool / action** | Routed to the CORRECT specialist + picked the CORRECT action; didn't over-act (draft/send when it should ask) or under-act (advise when it should execute) | correct specialist + act-vs-advise choice |
| **Next-action-set quality** | The proposed next actions are the RIGHT and EFFICIENT set for the goal — not just "a next step exists" | plan QUALITY, not existence |

**The decision-quality group (last 3 rows) is the "business OPERATOR vs well-behaved chatbot" line.** It is
the HARDEST to measure — it needs each scenario to define the right-vs-wrong operational call as ground truth.
The current 53 lean conversational and UNDER-TEST it; a clearly-wrong call in an "acceptable" transcript is a
Tier-1 breaker (§2.6), but merely-suboptimal judgment on an under-specified scenario is NOT penalised (would be
noise). Proper decision-quality measurement requires authored JUDGMENT scenarios (§4).

## 4. Overfitting guard — the 53 eval is the THERMOMETER, not the objective
The objective is **generalization to unseen real owner conversations.** The 53-scenario pack only ESTIMATES it.
Guards, binding on every manager change:
- **Fix the general behavior, never the scenario's exact strings.** No teaching-to-the-test (no special-casing a
  scenario's phrasing to make it pass).
- **The trust-breaker rubric (§2.1) is behavior-general**, not scenario-specific — it applies to any conversation.
- **Hold-out + fresh scenarios**: keep a rotating held-out subset the manager is not tuned against; add new
  real-shaped scenarios periodically; a lift that appears only on the tuned set and not the held-out set is
  overfitting, not progress.
- **Judgment scenarios (BUILD NEEDED)**: decision-quality (§3 last 3 rows) can only be measured on scenarios
  that DEFINE the right-vs-wrong operational call as ground truth. The current 53 lean conversational and
  under-test it — author JUDGMENT scenarios (a clear right call + tempting wrong calls: a finance question that
  must NOT go to sales; a "just asking" that must NOT trigger a send; a situation that DEMANDS execution not
  advice) where a well-worded WRONG decision MUST score as a fail. Hold these out too.
- **Reality check**: if the eval number rises while real conversations don't improve, the metric is being gamed —
  distrust it.

## 5. Relationship to the VT-611 conjunctive gate (the mismatch Fazal flagged)
The VT-611 gate = every dim ≥4 AND mean ≥4.5 (a STRICT conjunctive bar). It is a useful HIGH-BAR internal
thermometer, but it is NOT the acceptance objective and OVERSTATES failure (a 5,5,5,5,4 = mean 4.8 scenario
FAILS it). The acceptance objective is the two-tier bar in §2. Both are reported side-by-side (§ re-score), but
**Tier-1 count=0 + Tier-2 ≥90% is the target going forward.**

## 6. First measurement, both metrics side-by-side (2026-07-09, same 53 transcripts)
Re-scored the SAME 53 gate transcripts (opus per-transcript classification against §2) vs the conjunctive gate:

| Metric | Number | Target |
|---|---|---|
| **Conjunctive gate** (every dim ≥4 AND mean ≥4.5) | **45.3%** (24/53) | — (not the objective) |
| **Tier-1: trust-breaker-free** | **79.2%** (42/53 clean; **11 have a trust-breaker**) | 100% (0 breakers) |
| **Tier-2: quality-acceptable OF clean** | **97.6%** (41/42) | ≥90% ✓ ALREADY MET |
| **Fully acceptable** (clean AND quality) | **77.4%** (41/53) | — |

**Read:** on the RIGHT metric the manager is ~77% acceptable, not 45% — the conjunctive gate nearly halved the
apparent quality. And Tier-2 is ALREADY met (97.6%): **when the manager doesn't trust-break, it's almost always
good.** So the entire gap is the **11 trust-breakers** — a finite, concrete do-or-die list, not a vague "raise the average."

### The 11 trust-breakers (the whole target), by cluster
- **Loop/stall — 7** (ask_owner_resume, efficient_no_overstep, topic_switch_winback, delegation_empty_cohort,
  m_hinglish_winback, bilingual_hinglish, longhaul): the "I'm on it → never delivers" / verbatim-repeat disease.
- **Ignored speech-act — 5** (cross_tenant_friend, efficient_no_overstep, m_fabricated_campaign_sent, longhaul,
  m_hinglish_winback): a direct question/correction got a canned message or a non-answer. (overlaps loop cluster)
- **Fabrication — 2** (hinglish_conversation, longhaul): **INVENTED PRICING** — "free trial", "viabe.in",
  "₹999/month" when asked cost in Hinglish (real = ₹5000/agent, no free trial). A hard trust-breaker.
- **Impossible promise — 1** (gbp_connect_honest_capability): promised a GBP connect walkthrough — GBP is NOT an
  owner-authorizable connect (only shopify + google_sheet are).

### What eliminates them (the concrete path to Tier-1 = 0)
- ~9 loop/stall + ignored → the **emission/progression fix** (VT-629 dispatch rule + the emission rewrite).
- 2 fabrication + 1 impossible-promise → a **capability-grounding rail**: no invented pricing/domain (source
  pricing from config), no promising a connect the platform can't do. Deterministic, count=0.
Two fixes clear the whole list. Delta measured on re-run after each lands (not guessed).

### 6.1 Decision-quality re-score (2026-07-09, after §2.6/§3 upgrade) — Fazal's concern CONFIRMED
Re-scored the same 53 on decision/tool/plan judgment ONLY. Found a wrong-call class the conversation rubric
scored "acceptable": **OVER-ACTING**. 3 clearly-wrong; **2 were in the previously-"acceptable" 41**:
- **delegation_analytical_routing** [NEW breaker]: owner asked "WHICH customers stopped buying?" (a question) →
  manager drafted a campaign + fired the approval template, never answered which/how many. Over-act.
- **m_conversation_followup_referencing_lapsed_customers** [NEW breaker]: owner asked a COUNT → manager drafted
  + armed a campaign instead of answering "2 of your 8". Over-act.
- **m_conversation_topic_switch_winback_detour** [already a loop breaker]: explicit "draft a winback" → manager
  UNDER-acted (stalled "I'm on it", no delegation). The mirror failure.
**Impact: fully-acceptable 41→39 (77.4%→73.6%).** The manager acts, but sometimes acts WRONGLY — a chatbot-that-
acts, not yet an operator. Decision/action breakers (~4 of the now-13): the 2 over-acts + gbp_connect (wrong
tool) + topic_switch (under-act). This is a DISTINCT third fix:
- **Speech-act gate** (stocktake step 3, now data-backed): a question / count / status turn gets ANSWERED, at
  most an OFFER to draft — NEVER silently draft+arm a campaign. Fixes the over-act class.
Updated fix map (3 fixes for the trust-breakers): emission rewrite (~9 loop/stall+under-act) + capability
grounding VT-630 (3 fabrication/impossible) + speech-act gate (2-3 over-act).

## 7. The management mandate — what "run operations unsupervised" (§1) concretely requires
Added Fazal 2026-07-10. §1–§6 are the reactive floor (respond without breaking trust). §7 is the PROACTIVE
manager. **These SIT ON the Tier-1 = 0 foundation — a manager that plans, delegates and validates is worthless
if its outcome-reporting fabricates.** (The verifier work in flight now — a genuinely-successful send must not be
reported as "couldn't finish" — is literally the first brick of 7C. Trust-breakers=0 is the prerequisite, not a
parallel track.) NOTE: 7A–7C are largely NET-NEW capability — today's TM is reactive-conversational; proactive
planning + impact-validation do not substantially exist yet. This section is the target, not current behavior.

### 7.0 Foundational principle — the LLM brain is central and irreducible (Fazal 2026-07-10)
Every decision the Manager makes — what to do with an incoming event, which task to run, which method/modality,
which scope — is made by the BRAIN reasoning. There is **NO hardcoded scenario/action logic that decides in the
brain's place**; we do not pre-program business responses. The Manager MAY develop its OWN rules/heuristics
through MEMORY + LEARNING and apply them — but even applying a learned rule is brain-mediated, not a static
branch. Deterministic code has exactly two roles and NEITHER is deciding: (a) **SENSE** — detect events/changes
and trigger the brain; (b) **GUARD effects** — the Pillar-7 rails check the brain's CHOSEN action against hard
constraints (consent, approval, eligibility). Gates CHECK; the brain THINKS. "The Manager cannot function without
his brain; the LLM is the brain." This reconciles with the effect-boundary: rails constrain effects, they never
make the business decision.

### 7A. PLAN — set the agenda, not just react
- **Monthly plan:** propose a month-level business plan (goals + initiatives) grounded in the tenant's real data
  and business type; **revisable as day-to-day conditions change** (new data, outcomes, owner input) — a living
  plan, not a static document.
- **Daily plan:** each day, a concrete "what we do today" derived from the monthly plan + today's conditions.
- **Acceptance:** grounded (no invented targets/₹ — §2.1 fabrication applies), specific + actionable, ADAPTIVE
  (re-plans on changed conditions; doesn't cling to a stale plan), owner-visible + steerable.
- **Boundary:** the TM PROPOSES and drives the plan; the owner can steer/veto. Plan items with effects
  (send/spend) execute through the effect-boundary (7C), never on the plan's own authority.

### 7B. LEAD — decompose, allocate to the right agent, drive to done
- Break goals/plan into tasks; allocate each to the CORRECT specialist agent; drive each to a REAL completed
  outcome (never "I'm on it" → silence).
- **Acceptance:** right-agent-for-the-task (§3 right-tool), tasks actually COMPLETE with the result surfaced to
  the owner (§3 delegation-and-surfacing), effective (advances the goal). A delegated task that silently never
  executes = Tier-1 breaker (§2.2 / §2.3).

### 7C. VALIDATE — judge the outcome by business impact, approve / disapprove
- After an agent completes, the TM validates the OUTCOME against the intended business impact and decides:
  accept / redo / escalate / flag.
- **Acceptance:** the judgment is grounded in the ACTUAL outcome (real audit facts — never a fabricated "done"),
  and the accept/reject call is sound vs the business impact.
- **Boundary — impact-graduated autonomy, NOT unbounded self-approval:** the TM may validate + approve
  LOW-impact, reversible outcomes autonomously; HIGH-impact / IRREVERSIBLE actions (real customer sends, spend,
  anything the owner must own) STILL gate to owner / VTR-human approval. **Pillar-7 holds — this does NOT loosen
  the no-send-without-approval invariant.** Autonomy graduates per-capability as accuracy is proven (Track-C); it
  is not granted wholesale.

### 7D. AUDIT — every decision, reason, thought, action logged + reviewable
- Every TM decision logs: the DECISION, the REASON/why, the underlying THOUGHT, and the ACTION taken — so any
  decision can be audited and reviewed to understand WHY it was made.
- **Acceptance:** complete (no silent decisions), captures the RATIONALE not just the action, human-reviewable,
  immutable enough to trust for audit.
- **Substrate:** VT-514 audit/trace log + VT-515 debug log + VT-516 viewer exist. **Gap-check:** confirm they
  capture the REASONING / thought, not only the action taken — the "why" is the new requirement.

### 7F. OPERATE CONTINUOUSLY — modular sensing layer + reactive Manager as its control plane
**Architecture decision (Fazal 2026-07-10 — chosen over a monolithic proactive loop):** the Manager stays
REACTIVE (preserves §1–§6 + the current build). Continuous operation is a SEPARATE, MODULAR sensing layer —
independent pollers / listeners / watchers / schedulers — that runs on its own and TRIGGERS the Manager with the
data when it detects something (event, data ingestion, schedule fire, external signal). The Manager then reasons
(brain, §7.0) about what to do with that data by its type / source / value. Specialized agents stay reactive; the
Manager is the reactive DECISION-MAKER **and** the CONTROL PLANE over the sensing layer. (Chosen because it
scales on cost, reuses the reactive core rather than rewriting it, is auditable, and extends the existing
DBOS/scheduler/reaper infra instead of fighting it — no capability is lost vs the monolith.)
- **Control plane:** the Manager can SET / UNSET / DEFINE watchers/schedulers at runtime. "Decide the method" =
  the brain chooses which sensing mechanism to instantiate (schedule / event-trigger / webhook / callback / poll)
  from the runtime's bounded menu. It does NOT invent scheduling code at runtime.
- **Scope reasoning (general-vs-specific — Fazal's example):** on installing a watcher the brain decides its
  SCOPE — a SPECIFIC watcher (this one order's payment) vs a GENERAL one (all pending-payment orders) once it
  recognizes a pattern — and CONSOLIDATES duplicates into a general watcher rather than spawning N specific ones.
  **TENANT-SCOPED ONLY (Fazal's call):** a "general" watcher spans one tenant's own data, NEVER across tenants
  (RLS / data isolation).
- **Lifecycle — no leaked or duplicated watchers (first-class acceptance criterion, Fazal's call):** every
  watcher is LAYERED for teardown — self-terminates when its condition resolves + a TTL backstop + a background
  reaper sweeps stragglers; the Manager can also explicitly unset. Installing is easy; guaranteed teardown is the
  hard part.
- **The brain reasons on EVERY trigger (Fazal's decision, §7.0):** when a watcher fires a real event, the BRAIN
  decides what to do — there is NO hardcoded action rule handling the event in the brain's place. The
  deterministic sensing layer's only jobs: (a) DETECT — control WHEN a trigger fires (a no-change hourly poll
  does NOT wake the brain; a status CHANGE does — this is how brain-on-every-trigger stays affordable), and
  (b) EXECUTE on rails once the brain has decided.
- **Effect-boundary unchanged:** a self-initiated effectful action (self-triggered send/spend) STILL passes
  owner/VTR approval (Pillar-7, §7C). Self-initiation is not a back door around approval.
- **Substrate:** DBOS workflows, scheduled pollers, the orphan-reaper, and the apify ingestion methods are the
  existing sensing substrate — modular EXTENDS them (Manager-programmed watchers + scope reasoning + lifecycle)
  vs today's FIXED crons. NET-NEW + roadmap AFTER trust-breakers = 0.

**Worked example (payment-pending, Fazal's):** Manager reviewing an order sees payment-pending → brain decides
to install an hourly payment-check scheduler and reasons scope: one order (specific) or many (one general,
tenant-scoped) → scheduler polls hourly (deterministic SENSE; still-pending = no brain call) → payment arrives =
TRIGGER → brain reasons what to do next (mark paid / thank the customer / update the plan / consolidate) →
watcher self-terminates (+ TTL + reaper backstop).

### 7G. Measurement
7A–7C + 7F are LONGITUDINAL (multi-day / event-driven), not single-turn — the 53-scenario pack does not test
them. Measured by: the 10-journey simulation (`.viabe/journey-sim-spec.md`) for lead + validate within a journey,
plus authored multi-day PLANNING + CONTINUOUS-OPERATION scenarios (BUILD NEEDED) — a monthly plan is proposed,
ADAPTS to an injected condition change, decomposes into sound daily actions, and the Manager self-initiates the
right task via the right modality on an injected event/schedule (not an owner message). Tier-1 = 0 (§2) applies
to every planning / leading / validating / self-initiated action.
