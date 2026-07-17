> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# Team-Manager Rebuild — Canonical Design (Cowork architect input, Fazal-ratified FOLD-IN 2026-06-28)

Owner-facing brain rebuild: from a thin router (CL-24) to a reasoning **Business-Executioner / Team-Leader** that understands owner intent and delegates to domain specialists — **inside fixed safety rails**. Fazal folded this into the single sign-off: the win-back send + sign-off WAIT until the new brain's live e2e re-drive is clean.

## 0) The central principle (the line we do not move)
"Nothing hardcoded/predefined" = **dynamic BEHAVIOR/REASONING/CONVERSATION**; the **safety/correctness RAILS + the definition of "prereqs satisfied" stay DETERMINISTIC and non-bypassable.** A CEO improvises strategy but cannot wire money without sign-off. Dynamic executive brain INSIDE fixed rails.

## 1) Target shape — supervisor-with-roster, minimal v1
LangGraph: a **Team-Manager reasoning node** (classify owner intent + frame as the owner's business manager + handle-directly vs delegate) + **specialist sub-graphs** with structured handoffs.
- **v1 roster:** Team-Manager supervisor + **onboarding-conductor** (core new build) + **sales-recovery** (exists — wire as handoff, do NOT rebuild) + **connect/integration** (a FLOW the supervisor hands to). Finance/cash-book/marketing = later additions; the design must make adding a specialist cheap (a sub-graph + a registry entry, not a refactor).
- **Cost/latency rail:** the supervisor handles simple turns DIRECTLY (one cheap call). No roster fan-out on "Hi". Spin a specialist only when intent warrants.

## 2) CL-24 supersession (new Standing)
**brain = a reasoning Team-Manager that understands owner intent and delegates to domain specialists.** Still NOT the domain executor (specialists are); still NOT the writer/sender (tools own writes); gates are deterministic non-bypassable rails.

## 3) Onboarding — dynamic conversation, deterministic completion
Scripted-queue → brain-conducted. The onboarding-conductor decides the next question dynamically, BOUNDED by the declarative **prereq registry** (agent-activation-prereqs): the registry defines WHAT must be collected; the brain decides HOW/wording. Keep `onboarding_journey` STATE for resumability. **"Complete" stays a DETERMINISTIC check** (GST-verified + ≥1 connector + ≥1 customer + consent) — never the brain's vibe.

## 4) The rails contract — non-negotiable, BUILD FIRST
Gates are NOT prompt text — they are TOOLS/GUARDS the brain MUST route through. The brain emits an INTENT ("send this win-back"); the effect runs through the guarded tool (`agent_send_draft` Gate-0) which deterministically enforces consent/opt-out/onboarded/owner-approval. **The brain has no code path to any side-effect except via a guarded tool.** Build the rail harness FIRST, then the brain INSIDE it. The no-send-without-owner-approval + DPDP/consent rails are existential if bypassable.

## 5) Build order (exec_order) → VT rows
| exec | VT | Row |
|---|---|---|
| 1 | **VT-460** | **Rail harness** — gates as deterministic tool-guards; brain has NO side-effect path except via a guarded tool. Adversarial-verify non-bypassability (attempt to make the brain send / skip consent / self-mark-onboarded → structurally impossible). Foundation; nothing lands until proven. |
| 2 | **VT-461** | **Team-Manager supervisor node** — reasoning brain: classify intent, frame as business manager (NOT customer-service), handle-directly vs delegate, structured handoffs, cheap single-call for simple turns. Supersede the CL-24 router prompt. |
| 3 | **VT-462** | **Onboarding-conductor** — dynamic brain-conducted onboarding bounded by the prereq registry; keep `onboarding_journey` state; "complete" = deterministic check. |
| 4 | **VT-463** | **Handoffs** — wire existing Sales-Recovery as supervisor→SR handoff (do NOT rebuild SR); connect/integration as a flow the supervisor hands to. |
| 5 | **VT-464** | **New-brain live e2e re-drive** — clean tenant: owner "Hi" → Team-Manager onboarding (dynamic) → connect → ingest → SR-detect → L2 armed → send-READY. Fix defects. THEN Fazal's single sign-off. |

## Finished first (independent, DONE + pushed d511ac2): persona-leak context + onboarding-contamination reset (owner never gets customer-service); VT-458 16-defect sweep; VT-459 brand colors.

Source: Cowork 20260628T204000Z design + Fazal FOLD-IN 20260628T204500Z. Build autonomously to exec_order; Cowork audits-after; raise blockers to Fazal. NO send until the new-brain re-drive + sign-off. main untouched.

## 6) AUTONOMY MODEL (Fazal 2026-06-28 — binding, reshapes §4)
The owner does NOT babysit / mentor / monitor. The Agent Team RUNS THE BUSINESS autonomously and makes it better for the owner. The owner is reached ONLY in **extreme scenarios**, and ALL owner communication is **WhatsApp-only**.

Reconciles the §4 rail "no-send-without-owner-approval" — it is NOT per-message approval:
- **Deterministic safety rails (unchanged, non-bypassable, AUTOMATIC — no owner in the loop):** consent allowlist + opt-out, send caps/budget, onboarded-gate, GST/ownership verify. These are the bounds the team operates WITHIN; the brain has no code path around them. (Scenario set D stays 100%.)
- **Action authority = owner POLICY granted at onboarding** ("run win-backs to lapsed customers within these bounds / caps / tone"), then the team acts AUTONOMOUSLY inside that policy + the safety rails. Not a per-send tap.
- **Owner escalation = EXTREME scenarios only** (anomaly, high-stakes/irreversible decision outside policy, complaint, repeated failure, policy-boundary judgment). Escalation channel = WhatsApp, concise.
- **Launch sign-off (separate, one-time):** Fazal validates the FIRST real send at sign-off (VT-464). Steady-state ≠ the launch gate. After sign-off + the granted policy, the team sends autonomously within the rails.

Design implication: the supervisor (VT-461) defaults to ACT (within policy + rails), not ASK. "Ask the owner" is a last-resort escalation tool, gated to extreme criteria — NOT the default for routine business actions. The brain must be biased to run the business, not to seek permission. The rail harness (VT-460) enforces the SAFETY bounds deterministically so autonomy is safe WITHOUT per-action owner approval.

## 7) FULL-SIX MANAGER + division correction + extended rails (Fazal 2026-06-28, supersedes §1 v1-roster scope)
**Remit = the WHOLE business:** Sales, Marketing, Finance, Accounting, Tech, Cost-Optimisation. Sign-off WAITS until all six lanes are built + wired (Fazal chose full-six). Build the lane-independent FOUNDATION now; lanes build to CHARTERS (Cowork drafts, Fazal ratifies advise-vs-act on Finance/Accounting/Cost-Opt); PROVE each lane incrementally (not big-bang-then-validate).

### Division of intelligence (211500Z correction — load-bearing)
- **Manager = SITUATION + OUTCOME.** Reads business situation/context (from the KG/business-profile) + decides/SUGGESTS the OUTCOME that benefits the business + WHICH specialist + arbitrates cross-functional tradeoffs + monitors outcomes. Does NOT prescribe the action. Outcome-accountable. **Never needs domain expertise** (never picks the action).
- **Specialist = ACTION.** Takes {situation, desired outcome, context-slice, data} + decides the ACTION itself using its domain expertise (the expertise — incl. WHAT to do — lives in the agent). Action-accountable. Lane-scoped; does NOT hold cross-functional strategy.
- **Handoff = TWO-WAY:** manager→agent {situation, outcome, context-slice, data} (NOT an action plan); agent→manager: if the outcome is infeasible/unwise in-lane, the agent PUSHES BACK + proposes a better outcome before acting (rail-gated).
- Business knowledge/context lives in the **KG/business-profile** (the moat, VT-466): manager reads/writes; specialists get scoped slices.

### Rails — EXTEND to business-impact (VT-467, extends VT-460)
- Compliance rails (unchanged, non-bypassable): DPDP/consent, ownership, GST, opt-out, no-customer-send-without-owner-approval.
- **NEW business-impact rails:** spend money / customer send / external commitment / config-integration change → owner-approval-gated guarded tools, THRESHOLD-based, **DECAYING-HITL** (approval loosens as the owner grants autonomy + the manager earns trust — REUSE the existing VTR decaying-HITL). Manager has NO code path to a consequential side-effect except via a guarded tool. Same guarded-tool framework as VT-460's customer-send choke; business-impact actions plug in per-lane.

### Roster registry + handoff protocol (VT-465 — the spine all six plug into)
Standard specialist interface + a registry so adding a lane = a sub-graph + a registry entry + its tool-set + its prereq registry (NOT graph surgery). REUSE make_spawn_tool + build_supervisor_graph + routing.

### The six lanes (build to charters; VT mapping)
| Lane | VT | First-cut charter | Rail |
|---|---|---|---|
| Sales | VT-463 (SR handoff, exists) | win-back lapsed → repeat/upsell/re-engage | no send w/o approval + consent |
| Marketing | VT-468 | campaigns, festival offers, segments, content drafts | no send/spend w/o approval |
| Finance | VT-469 | cash-flow, receivables/payables, payment reminders, margin/pricing input | ADVISE + owner-approved reminders; NEVER moves money |
| Accounting | VT-470 | bookkeeping/categorization, GST+tax-summary prep, invoice/expense, reconciliation | PREPARE/summarize; does NOT file/transact |
| Tech | VT-471 | store/website/listings (GBP/Shopify), integrations, setup | config/integration changes owner-gated |
| Cost-Opt | VT-472 | wasteful spend, subscriptions/vendor cost, marketing ROI, savings | ADVISE; any cut owner-gated |

### Revised build order (foundation now; lanes to charters)
exec-1 VT-460 rail harness (compliance + the guarded-tool framework) → VT-467 business-impact rails (extends it) → exec-2 VT-461 supervisor (situation+outcome+which-specialist+tradeoffs, reads/writes KG) → VT-465 roster registry + handoff protocol → VT-466 KG/business-context store → exec-3 VT-462 onboarding-conductor → VT-463 Sales/SR handoff → [CHARTERS] VT-468..472 lanes (incremental, each verified incl. its rails) → VT-464 full live e2e re-drive → Fazal sign-off.

## 8) RATIFIED CHARTERS + autonomy rulings (Fazal/Cowork 2026-06-29; supersedes §7's first-cut)
### Autonomy hardening (Cowork audit, APPROVED + 2 hardenings)
- A2: **"within policy" = a DETERMINISTIC bound-check (a rail/guard), NOT the brain's self-judgment.** The onboarding-granted policy = machine-enforceable bounds (segments, frequency caps, spend ceiling, allowed action types). The brain cannot reason itself out of policy.
- A3: **escalation = concrete deterministic triggers** (repeated rail-trip; spend/volume anomaly vs baseline; out-of-policy irreversible attempt; complaint/opt-out surge; repeated specialist failure; any money-movement/return-filing request; send-quality flag). WhatsApp-only, concise.
### SEND ruling (Fazal): DECAYING CHECKPOINT
The customer-SEND action EARNS autonomy — tight owner visibility on the FIRST sends per new tenant/campaign → decays to full autonomy once proven safe (reuse VTR decay + owner-approval). NOT per-send-forever. Build as the send-path's autonomy curve. (VT-474)
### The 6 lane charters (v1 = advise/act-within-policy; FUTURE scope documented, do NOT build future-autonomy now)
| VT | Lane | v1 scope | Rail | FUTURE (architect, don't build) |
|---|---|---|---|---|
| 468 | Sales | win-back (SR exists)→repeat/upsell/re-engage | send: decaying-checkpoint + consent/caps | — |
| 469 | Marketing | campaigns, seasonal/festival offers, segments, content | send/spend within policy; send: decaying-checkpoint | — |
| 470 | Finance (ADVISORY always) | cash-flow, receivables/payables, payment-reminder drafts, margin/pricing; SUGGEST money movement, IDENTIFY losses/debt/loss-reduction | NEVER moves money; reminders=sends(decaying-checkpoint) | stays advisory |
| 471 | Accounting (v1 PREPARE-only) | bookkeeping, GST+tax-summary prep, invoice/expense, reconciliation | prepare/summarize; does NOT file/submit | gated behind explicit Fazal grant + regulatory auth: FILE returns, balance sheet, SUBMIT GST |
| 472 | Tech | store/website/listings (GBP/Shopify) health, integrations, setup | config/integration changes owner-gated (business-impact) | — |
| 473 | Cost-Opt (v1 ADVISE) | wasteful spend, subscriptions/vendor cost, marketing ROI; resource recalibration (human+non-human: sharing/sharding/parallel/full-utilization) | v1 SUGGEST; acting owner-gated | act on recalibration (owner-gated, expandable) |
### Build order: VT-474 (policy-bound determinism + escalation + send-checkpoint rails) → lanes VT-468..473 incrementally (each: land → adversarial-verify incl rails + policy-bound determinism → Cowork audit → next) → VT-464 full live re-drive → Fazal sign-off.
