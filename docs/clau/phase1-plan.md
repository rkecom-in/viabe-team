# Viabe Team — Phase 1 Plan: Dependable Business Execution
*Status: **LOCKED / Standing** (Fazal 2026-07-01) — the single governing Phase-1 contract. Scope-locked: no new Phase-1 features after lock, only closure/verification/launch-defect fixes. Ledger: CL-2026-07-01-phase1-locked.*

Phase 1 is complete only when a verified business owner can state a business objective on WhatsApp, Viabe can understand it, create durable work, delegate it, execute within policy, verify the result, and report the outcome — without engineering intervention. The plan below is the single path we are executing to reach that bar and, from the first customer, to begin earning true autonomy.

---

## Guiding principles

1. **Reasoning is free; eligibility and effects are deterministically validated.** The manager may plan, reason, sequence, and self-evaluate without restriction. Execution eligibility and every external effect are checked by deterministic, non-bypassable controls — prerequisite validation, schema validation, legal task-state transitions, hard limits, evidence requirements, capability checks, and the effect rails (customer sends, money, personal data, consent/opt-out, ownership, commitments, configuration). Controls constrain what may *execute*, never what may be *thought*.
2. **Observability is for detection and learning, not safety.** The activity log records decision summaries, retrieved evidence, tool calls, routes, and outcomes — observable facts, not private chain-of-thought. It detects problems and mints the learning signal; it is not itself a control.
3. **Concierge Mode is the launch posture and the learning engine.** At launch a trained human reviewer (the "Viabe Team Rep", VTR) reviews every consequential action. Because the reviewer judges every action, each becomes a labelled signal used to earn autonomy per capability. Concierge is how autonomy is produced — not a holding pattern and not a permanent ceiling. It is day-zero of a decaying human-in-loop model: independence is a measured threshold (rising confidence + falling escalation rate), never a date. Fazal is VTR #1; a second reviewer cannot be added until per-tenant assignment scoping is enforced (a reviewer sees only assigned tenants, with customer data encrypted from the reviewer).
4. **Evidence, not assertion.** Real-service proof is mandatory for anything touching an external API, persistence, or a deployed workflow — no such item closes on mocks, structural inference, or an HTTP status. Pure deterministic internal components may close on exhaustive automated proof, provided the integrated journey they sit in is live-proven end to end.
5. **Fail closed.** Any unknown terminal state, missing evidence, orphaned task, or delivery ambiguity blocks autonomous action and surfaces for review.

## Launch posture, date, and gates

- **Launch = Concierge Mode.** The reviewer approves every consequential action; deterministic rails are enforced; advisory functions are labelled advisory; unverified tools and connectors are disabled.
- **Date rule:** quality gates take priority through **31 July**; Concierge Mode launches on **1 August**. The non-waivable gates are never waived and **override the date** — if any is red on 1 August, launch waits until it is green. (Several gates have third-party lead times, so 1 August is a target-if-green.)
- **Autonomy is earned per capability** from measured clean outcomes — never unlocked by elapsed time.
- **Non-waivable gates (never waived):** privacy, tenant isolation, ownership verification, consent, send safety, data-subject rights, and production-environment readiness.
- **Scope lock:** after lock, no new Phase 1 features — only closure, verification, and launch-defect fixes.

## Function scope at launch

| Function | Phase 1 mode |
|---|---|
| Sales Recovery | the first function eligible to graduate from Concierge to autonomous; every consequential action is still reviewed at launch |
| Marketing | prepare and propose; any customer send uses the same send rails |
| Finance | advisory |
| Accounting | prepare-only |
| Tech | owner-authorised changes only |
| Cost Optimisation | advisory |

Advisory functions are never described as autonomous execution.

---

## Track A — Production and external readiness
*Starts immediately and runs in parallel; longest third-party lead times; binds the launch date.*

- Provision the production database in the target region (Mumbai) under strict secret handling: no local production secrets, explicit environment on every command, sentinel verification, and founder-authorised migrations.
- **Ownership migration (not a blanket backfill):** existing tenants move to verified-ownership status **only from documented reviewer (VTR) evidence** — never automatically because a tenant was previously "verified" under the retired self-declared mechanism.
- Complete the privacy / DPDP counsel review before admitting any design partner.
- **Data-subject rights — build before canary:** erasure/purge exists; the **data-principal access and correction flow is not yet built and the Grievance Officer is not yet appointed.** Build the access/correction path and appoint the officer, *then* run the canaries.
- Provision a real, merchant-owned WhatsApp Business Account via Meta Embedded Signup, on a dedicated number.
- **Fix the onboarding welcome delivery:** the current welcome template is declined by Meta as MARKETING (Twilio error 63049). Reclassify it to a compliant UTILITY template and obtain Meta reapproval; verify real delivery end to end (delivery callback, not acceptance alone).
- Complete real Shopify and Google OAuth with merchant accounts; verify reads after each callback.
- Verify Twilio production paths end to end: inbound, owner reply, template send, delivery callback, opt-out, failure callback.
- **Billing (does not gate admission):** admission opens on a 30-day, no-card trial, so payment does not gate design-partner admission. The payment subscription + webhook paths must be **production-proven before the first trial converts to paid** (day 30+).
- Run data-subject-request canaries on production once the flows above exist: access, correction, erasure, activity/failure-log purge, outbound-message redaction. Unavailable credentials or skipped checks are failures.

## Track B — Operator spine
*Each package requires unit, integration, adversarial, recovery, and real-service canary acceptance on the deployed environment.*

**B1 — Truthful terminal outcome and owner notification** *(first)*
- **Terminal outcome** — every run resolves to exactly one of: `completed_with_effect`, `completed_no_action`, `failed`, `escalated`, `cancelled`. (Waiting and in-progress states — `running`, `waiting_owner`, `blocked` — are task states in the B2 state machine, not terminals.)
- **Owner notification is a separate, linked, asynchronous state:** `owner_notification_status = not_required | pending | accepted | delivered | failed`. `not_required` — internal runs such as callbacks, scheduled maintenance, and internal workflow steps — demands a deterministic reason and is never a silent default. A transport SID proves *acceptance*, not delivery; delivery/failure arrives later by callback. A task may be execution-complete while its notification is durably `pending`; it is never presented as owner-acknowledged until the delivery callback confirms it.
- **Communication status is `delivered | failed_incident_open`, and applies to every owner-facing task terminal (not internal runs).** A failed notification runs an escalation ladder — retry budget → an alternate compliant WhatsApp path where one exists → reviewer alert → an **owned** owner-contact incident. Runtime invariant: every failure creates an owned incident, never silence. An open incident is resolved operationally, but **communication is not complete until delivery is confirmed** — a `failed_incident_open` terminal is never presented as owner-reached.
- A bare "completed" is forbidden without either a verified effect or an explicit no-effect classification.
- Any unrecognised terminal converts to an explicit failure and escalation — never a silent stop. Built on the existing activity and failure logs.

**B2 — Durable manager task contract (one canonical storage decision, locked)**
- **New canonical tables `manager_tasks` and `manager_task_steps` hold manager-task state.** The existing orchestration is reconciled under them, not replaced: **`business_plan`** remains the long-term roadmap; **`agent_work_items`** remains autonomous roadmap execution; **`pipeline_runs`, durable workflows, approvals, and effects become linked execution evidence.** No other table may independently claim manager-task status.
- Canonical hierarchy: `manager_task → task_step → pipeline_run / workflow / effect`.
- The task holds a structured, redacted objective, acceptance criteria, source-message reference, status, assigned function, policy reference, current step, evidence references, and an idempotency key. **Raw inbound prose is request-scoped and not persisted** (inbound message bodies are dropped); only the structured/redacted objective, hashes, and source-message references persist, unless a separately authorised tenant conversation policy exists.
- Enforced legal transitions: `clarifying → planned → running → waiting_owner/blocked → verifying → completed/failed/cancelled`, with compare-and-set versioning so replay, duplicate events, or two workers cannot regress a terminal state.
- An orphan detector: every non-terminal task has a runnable step, a durable wait, or an explicit blocker.
- **Privacy lifecycle (mandatory, in the same migration):** `manager_tasks` and `manager_task_steps` ship with tenant RLS plus `FORCE ROW LEVEL SECURITY`; DSR-purge registration in the same migration; a retention classification (lifetime-of-relationship, DSR-purge the sole deletion path); no raw phone/body/name columns; reviewer access only through de-identified, assignment-scoped views; and defined referential-deletion behaviour for linked execution evidence.

**B3 — Two-way delegation through the guarded effect pipeline**
- The handoff carries the manager-written situation, desired outcome, acceptance criteria, context references, and policy bounds. Empty manager framing is a contract failure — static defaults are not a valid handoff source.
- The specialist returns a **proposal or result** (status, action/proposal, pushback, evidence, unmet criteria, retryability, owner-action requirement) **to the manager, not to the end of the run.**
- The manager evaluates and decides: accept, revise the outcome, invoke the next specialist, clarify, or escalate. **Any accepted effect then runs through the existing deterministic guarded pipeline** (collapse → approval → execution) — the manager proposes and decides, but never bypasses the effect rails.
- Sequential multi-specialist work with a persisted ordered step plan replaces first-tool-wins; parallel execution follows once the sequential contract is proven.

**B4 — Clarification and continuity**
- Ask at most three concise questions per turn; persist unanswered questions with expiry; correlate replies by tenant, task, message reference, and active wait.
- Resume the same durable task after a reply, restart, delayed response, or link-out return.
- Maintain a compact running context — task summary, confirmed facts, open questions, owner preferences, recent decisions. Never re-ask confirmed information unless the owner corrects it.
- Route business-knowledge gaps to the de-identified reviewer; route authority, preference, and customer-identity questions to the owner.

**B5 — Verification and capability truth**
- A capability registry declares, per function and tool: live / advisory / disabled mode, prerequisites, effect class, policy rail, verifier, rollback, and environment availability. The manager may promise only capabilities marked live for that tenant and environment.
- Evidence before completion: a send requires an approved cohort, rail result, transport ID, and delivery/failure status; a database mutation requires a write receipt plus a scoped read-back; a connector requires an authenticated health read; a campaign requires a persisted plan, cohort count, approval, and execution receipt; advisory work requires a grounded result delivered to the owner.
- Per-function verification — no specialist self-certifies success.
- Owner-readable progress receipts; task state, evidence, failures, retries, and takeover controls surfaced in the operations console.

**B6 — Recovery, safety, and operations**
- Prove durable recovery at every boundary: before and after each model call, handoff, write, approval, send, verification, and owner delivery.
- Prove that duplicate events, worker or deploy restarts, timeouts, and delayed callbacks each produce exactly one effect.
- Deterministic retry budgets, exponential backoff, dead-letter state, and operator redrive.
- Confirm no action bypasses consent, opt-out, ownership, tax-ID, policy, send-checkpoint, spend, commitment, or configuration rails.
- Global, tenant, function, and campaign freeze, plus reviewer takeover.
- One no-orphan invariant across tasks, runs, workflows, approvals, and outbound sends.
- Immediate alerts on a silent terminal, orphaned task, cross-tenant reference, personal-data leak, repeated failure, or rail-bypass attempt.

## Track C — Autonomy and learning
*What makes the system autonomous and compounding. Capture and graduation machinery is live before the first tenant; individual capabilities graduate only once they earn it.*

**C1 — Effect-boundary determinism** *(a design principle applied throughout Track B, not a sequenced step)* — deterministic validation of execution eligibility and external effects; wide reasoning latitude everywhere else. Nothing outside the eligibility/effect boundary gates the manager's reasoning or planning.

**C2a — Self-handling and recovery** *(ships with B3/B6, before the soak, and is exercised during it)* — on a tool failure, unexpected result, or novel situation, the manager reasons a recovery (retry differently, try another path, re-plan) before escalating; escalation is the last resort.

**C2b — Confidence calibration** *(begins after captured outcomes exist)* — each decision carries a calibrated confidence signal measured against real outcomes; **model-reported confidence informs whether to ask or escalate but never, by itself, grants autonomy.**

**C3 — Learning loop (capture, then gated retrieval)** — an outcome/correction store captures every reviewer correction, owner accept/reject/edit, and downstream outcome, keyed to the decision and the observable knowledge in the activity log. **Capture substrate is built with B6 and runs during the technical soak** (so it is tested and generating data before tenant 1). **Retrieval activation is separate and later:** a correction influences a future decision only after it passes controls — tenant scope, provenance, correction authority, expiry, contradiction resolution, and evaluation. A reviewer mistake must never silently become policy. The correction store carries the same privacy lifecycle as the task tables — tenant RLS + `FORCE ROW LEVEL SECURITY`, DSR-purge registration in its migration, retention classification, no raw phone/body/name columns, reviewer access only through de-identified assignment-scoped views, and defined referential deletion.

**C4 — Per-capability accuracy graduation** — capabilities are defined as units (e.g. a win-back draft for a restaurant cohort), each with a risk class. Graduation requires all of: a minimum sample per capability and risk class; a lower confidence bound on the agreement rate (not a raw percentage); zero critical rail or safety violations; acceptable outcome quality, complaints, and opt-outs (from the business-observation window, below); recency weighting; and founder-approved thresholds set before the first graduation. A capability moves concierge → supervised-auto → full-auto only when it clears those bars, and demotes automatically on regression. Never time-based, never granted by self-report. All autonomy remains subordinate to the tenant's current owner policy.

## Track D — Business expertise and advice quality
*Safety and autonomy are worthless if the advice is mediocre. This makes Sales Recovery's output actually good.*

- A **curated, sourced Sales Recovery playbook** — real, attributable plays, not invented tactics.
- **Vertical applicability and exceptions** — which plays fit which business type, and where they don't.
- **No invented numerical claims** — the system never fabricates statistics, percentages, or benchmarks.
- **Advice freshness and provenance** — every piece of guidance is sourced, dated, and traceable.
- **Retrieval into specialist context** — the playbook feeds the Sales Recovery specialist at decision time.
- **Gold-set evaluation** — advice is graded against a held-out set for factuality, actionability, relevance, and tone; it must clear the bar before the capability is offered.
- **Initial acceptance bar:** roughly 100 reviewed notes, each carrying a tip, rationale, an optional sourced number, an applicability exception, provenance, and a review date. The gold set used for evaluation is held separate from the retrieval corpus.

## Operational contracts (launch foundations)

**Reviewer (VTR) contract** — Concierge Mode cannot launch on the phrase "trained reviewer." Stand up: reviewer training and certification; queue ownership; a review SLA (which bounds owner-response time); the de-identified information boundary (customer data encrypted from the reviewer); conflict-escalation paths; an audit trail of every reviewer action; per-reviewer capacity limits (which gate admission scaling); absence coverage (pending actions queue or halt when no reviewer is available — never auto-proceed); and the second-reviewer precondition (per-tenant assignment scoping) before anyone beyond Fazal is added.

**Owner-policy contract** — versioned owner grants: permitted customer segments and actions; frequency and spend bounds; expiry; revocation; and conflict precedence. Every effect is checked against the tenant's current policy, and **capability graduation is always subordinate to it** — a globally graduated capability still acts only within what this owner has currently authorised.

## Execution order

`B1 → B2 → B3 → B4 → B5 → B6 (+ C2a self-handling/recovery + C3 capture substrate) → technical soak → C2b confidence calibration → C3 retrieval activation → C4 graduation`. C1 is a design principle honoured throughout Track B. Track A and Track D run in parallel throughout. The reviewer and owner-policy contracts are launch foundations delivered alongside B4–B6.

## Verification programme

- Automated journey matrix: direct answer, ambiguity, clarification and resume, every specialist route, specialist pushback, sequential specialists, owner approval, reviewer handoff, hard limit, tool failure, delivery failure, duplicate event, restart, timeout, cancellation, opt-out, data-subject request, Hindi/Hinglish, media, and prompt injection.
- Tenant-isolation and personal-data tests across every new table, event, log, task, handoff, and viewer.
- One clean real production journey: signup → tax-ID verification → WhatsApp → onboarding → connector → ingestion → objective → Sales Recovery → checkpoint → send → callback → outcome report.
- One unhappy journey for every external vendor and every deterministic rail.
- **Technical soak (24–48h, pre-launch gate):** durability, sends, callbacks, tenant isolation, and recovery, with scheduled workflows, restarts, and induced vendor faults, and zero manual database repair. Code freezes at soak start; any critical fix resets the affected clock. The C3 capture substrate runs during this soak. A full 72h unattended soak is required before any capability graduates to full autonomy.
- **Business observation (separate, over the campaign window):** replies, conversions, opt-outs, and attribution measured over the applicable Sales Recovery window (days to weeks) after launch. This measures effectiveness, not durability; it feeds C4 graduation and is **not** a pre-launch blocker.
- Required results (technical): zero critical incidents, zero silent terminals, zero orphan tasks, zero duplicate effects, zero cross-tenant reads, zero unapproved sends, **zero open owner-contact incidents**, and every owner-facing task terminal at `communication_status = delivered` — the clean production journey must end in confirmed delivery; internal runs correctly marked `not_required` with a deterministic reason.

## Admission and rollout

- Admit tenant 1 (design partner); observe one complete technical execution cycle within 24 hours (business effectiveness is measured separately over the longer observation window).
- Admit tenants 2–3; observe 48 hours with no critical regression.
- Admit tenants 4–10 in two controlled cohorts; each starts with reviewer monitoring and send checkpoints.
- Autonomy expands per capability only from measured clean outcomes; elapsed time alone never unlocks it.
- Immediate rollback to concierge mode is always available without losing task state or owner context.

## Delivery control

- Reconcile the current state of code, migrations, deployments, and external dependencies before implementation begins.
- Maintain one requirements-traceability matrix: requirement → decision → implementation → automated test → live proof → evidence owner.
- Allocate all work-item and migration identifiers once, up front.
- Real-service canary acceptance is mandatory for external-API, persistence, and deployed-workflow items; pure deterministic internal components may close on exhaustive automated proof provided their integrated journey is live-proven. Nothing closes on a successful HTTP status alone.
- The delivery lead audits after landing; the implementer owns adversarial verification; the founder owns legal text, external accounts, production authorisation, policy thresholds, and final admission.
- Any unknown terminal, missing evidence, orphaned task, or delivery ambiguity fails closed and blocks autonomous launch.

## Definition of Done

Phase 1 is Done only when one real owner completes the full journey without engineering intervention; every accepted objective has durable state under the canonical hierarchy; every specialist returns a proposal or verified result to the manager and every accepted effect passes its deterministic rail; every completion has verified evidence; every owner-facing terminal reaches confirmed delivery, and any failure is an owned open incident (communication is not complete until delivered); the clean production journey ends in confirmed delivery with zero open owner-contact incidents; recovery is proven; the reviewer and owner-policy contracts are in force; Sales Recovery advice clears its gold-set evaluation bar; production, legal, and vendor gates are green; and the staged admission run completes without a critical incident. The **learning-capture and graduation machinery is live and capturing from the first concierge tenant** — with no requirement that any capability has yet graduated; graduation happens only once its evidence bar is met.
