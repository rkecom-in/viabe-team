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
