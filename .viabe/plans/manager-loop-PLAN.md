# Phase 1 Plan: Genuine Autonomous Team Manager

## Summary

Replace the current one-pass router with a durable, sequential management loop that can understand an objective, create and persist a plan, delegate each step, evaluate specialist returns, replan, pause for input or approval, verify completion, and report the outcome.

Phase 1 has exactly three true specialists: **Onboarding**, **Integration**, and **Sales Recovery**. Marketing, Finance, Accounting, Tech, Cost Optimisation and general Sales remain advisory Manager tools with no spawn routes or effect authority.

## Core Interfaces

- Introduce strict `ManagerPlan` and `PlanStep` models. A plan contains one objective, measurable acceptance criteria, and at most eight sequential steps.
- Extend `SpecialistHandoff` with `task_id`, `step_id`, `acceptance_criteria`, `policy_ref`, and attempt number.
- Replace the current return envelope with:
  `status = completed | needs_owner_input | blocked | failed`,
  `action_summary`, `outcome_summary`, `evidence_refs`, `effect_intents`, `owner_question`, `proposed_outcome`, and `reason_code`.
- Manager decisions become:
  `accept_step | revise_step | ask_owner | continue | complete | escalate`.
- Add `force_l3` to the VTR per-capability autonomy API. Existing demote/freeze/unfreeze remain. Tenant-wide takeover/release remains the emergency freeze; tenant-wide promotion is prohibited.

## Implementation Phases

### 1. Correct Scope and Tenant Security

- Restrict the runtime specialist roster and spawn-tool inventory to Onboarding, Integration, and Sales Recovery.
- Move the other six lane tool sets into an `AdvisoryToolRegistry` available only to the Manager. Remove their graph nodes, spawn tools and “specialist” prompt claims.
- Advisory tools may read, analyse, prepare or draft, but may not send, spend, commit externally, mutate configuration, or claim execution.
- Apply context-derived tenant resolution to every Onboarding and Integration tool. Model-supplied tenant IDs become ignored compatibility parameters; a missing ambient tenant returns a structured error.
- Filter the connector catalogue exposed to owners to **Shopify and Google Sheets**. Placeholder connectors, including Amazon, must be reported as unavailable and never offered as actionable.

### 2. Make the Task Spine Executable

- Allocate the migration number with the mandatory allocator before implementation.
- Extend `manager_tasks` with `queued` status, `plan_revision`, `terminal_outcome`, and `owner_notification_status`.
- Terminal outcomes are exactly:
  `completed_with_effect | completed_no_action | failed | escalated | cancelled`.
- Notification states are:
  `not_required | pending | accepted | delivered | failed`.
- Extend task steps with `plan_revision`, `specialist`, and `superseded` status. Add `advisory_tool` as a step kind and `pipeline_step` as an evidence kind.
- Store the typed step contract in redacted `detail`: situation, desired outcome, acceptance criteria, allowed effects and specialist return.
- Replace the current post-route task producer. A task is created when the Manager accepts an objective, before delegation; the complete ordered plan is persisted atomically.
- Preserve revisions by superseding unexecuted old steps and appending a new revision. Never edit historical completed steps in place.
- Serialize objective-bearing tasks per tenant. One task runs; later objectives queue. Direct questions and status queries remain answerable while work is active.

### 3. Build the Durable Manager Loop

- Retain deterministic ingress precedence for opt-out, DSR, delivery callbacks and approval replies.
- Route every other owner message through Manager triage with conversation history, business context, active tasks and pending questions.
- Direct FAQ/small-talk turns produce a completed owner reply without creating a task.
- Objective turns create a validated plan and start a DBOS `manager_task_workflow(task_id)`.
- The workflow repeatedly loads the current step, validates capability and prerequisites, dispatches one specialist, consumes its structured return and invokes the Manager decision node.
- `accept_step` records evidence; `continue` starts the next step; `revise_step` appends a revised plan; `ask_owner` opens a pending question and interrupts; `complete` verifies acceptance criteria; `escalate` creates an incident and reports honestly.
- Resume the exact task and step from owner replies, OAuth callbacks, approvals, scheduled retries and service restarts.
- Allow at most eight steps, two revisions per step and six Manager/specialist cycles per run. Exceeding a limit blocks the task and alerts VTR rather than silently terminating.
- Remove specialist-to-END wiring. Every specialist returns to `manager_review`; only the Manager can complete or advance a task.
- Keep the current graph behind `TEAM_MANAGER_LOOP_MODE=legacy|shadow|enforce` for rollback during rollout.

### 4. Make All Three Specialists Real

- **Onboarding:** Move conversation ownership from the pre-Manager journey interceptor into the Onboarding specialist. Reuse journey state, discovery, extraction and deterministic completion checks as tools. The specialist absorbs volunteered facts, corrections and skips, then returns either the next owner question or completion evidence. It cannot self-mark completion.
- **Integration:** Add context-scoped tools for reading phase state, starting OAuth, checking callbacks, pulling samples, proposing/confirming mappings, committing ingestion and scheduling recurring pulls. Shopify uses fixed mapping; Google Sheets uses the existing field-mapping reasoner. Every phase persists before replying and resumes through the Manager task.
- **Sales Recovery:** Preserve cohort detection, structured plan generation, self-evaluation, collapse and effect rails. Adapt its output to `SpecialistReturn`; the Manager must validate grounding and acceptance criteria before an approval or autonomous effect is armed.
- Each specialist receives only its scoped handoff and context. It chooses the action within its lane; the Manager specifies the outcome, not implementation instructions.

### 5. Autonomy and Effects

- Keep earned autonomy: Sales Recovery starts at L2, earns eligibility after the existing 20 clean approvals, and reaches L3 only through explicit owner opt-in.
- VTR may force one capability to L3, demote it, freeze it or unfreeze it. Forced L3 bypasses the earning threshold only; it never bypasses owner policy, consent, opt-out, caps, ownership, activation or effect rails.
- VTR tenant takeover freezes all capabilities, pauses dispatch and cancels open effect batches atomically. Release unfreezes them but does not promote them.
- Onboarding captures a versioned, machine-enforceable owner policy. Missing or malformed policy remains deny-all.
- All consequential effects pass through existing deterministic policy and approval gates. Unknown state, missing evidence or failed verification blocks execution.

## Verification and Acceptance

- Keep the complete existing suite green, including fresh-database migrations, RLS, web tests, lint and static tenant-access checks.
- Add graph tests proving multiple specialist steps execute in order and every specialist return reaches Manager review.
- Add restart tests at plan creation, specialist dispatch, owner-question pause, OAuth wait, approval wait, verification and notification.
- Add adversarial tenant tests proving arbitrary model-supplied UUIDs cannot read or write another tenant.
- Add E2E paths for: Onboarding→Shopify→Sales Recovery; Onboarding→Sheets→Sales Recovery; context retention; corrections; topic switching; specialist pushback; replanning; connector failure; queued objectives; duplicate webhooks; VTR force-L3; demotion; tenant takeover; and policy/consent/opt-out rejection.
- Assert that future-domain requests use advisory tools, create no specialist spawn, perform no effect and are described as advice.
- Run at least 120 synthetic server-side scenarios. All hard assertions must pass; 30 critical scenarios run three times; every transcript-judge dimension must score at least 4/5 with an overall mean of at least 4.5.
- Run real development canaries against merchant-owned Shopify and Google Sheets accounts, verifying callback, sample, mapping, ingestion, scheduled pull and restart recovery.
- Require zero silent terminals, zero cross-tenant access, zero unapproved effects, zero repeated in-context questions and zero unsupported capability claims.
- Persist a redacted evidence manifest containing code SHA, deployment SHA, scenario results, judge scores, canary references and transcript hashes.

## Rollout

- `shadow`: the new loop creates plans and decisions but the legacy graph owns replies/effects. Compare at least 50 conversations.
- `enforce-dev`: new loop owns development traffic; complete the full scenario and live-canary gates.
- `enforce-canary`: enable only for designated Concierge tenants with VTR monitoring and global freeze available.
- `production`: promote the exact verified SHA only after every acceptance gate passes. Rollback changes only the mode flag; additive migrations remain compatible.

## Locked Assumptions

- Three specialists only: Onboarding, Integration and Sales Recovery.
- Shopify and Google Sheets are the only fully supported Phase-1 connectors.
- Plans are sequential; no parallel specialist execution.
- Future business domains remain advisory Manager tools.
- Autonomy is earned, never time-based.
- VTR controls autonomy per capability and has a tenant-wide emergency freeze, but no tenant-wide promotion.
