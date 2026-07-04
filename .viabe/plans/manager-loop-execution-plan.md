# Viabe Team Phase 1: Implementation Execution Plan

## 1. Delivery Contract

Implement a durable Team Manager that can understand an owner objective, create a sequential plan, delegate to the correct specialist, evaluate the result, replan, pause and resume, verify success, and report the outcome.

The only Phase-1 specialists are:

1. `onboarding_conductor`
2. `integration_agent`
3. `sales_recovery_agent`

Marketing, Finance, Accounting, Tech, Cost Optimisation and general Sales remain Manager-held advisory tools. They must not appear as agents, spawn routes, graph nodes or autonomous capabilities.

Before implementation:

- Read the repository bootstrap files and reconcile against Git/PR reality.
- Allocate every VT row and migration number before parallel work begins.
- Preserve all deterministic safety rails.
- Do not combine this work into one large PR. Execute the packages below in order.
- Keep production on the legacy graph until the final promotion gate.

## 2. Required Types and Persistence

### Manager plan models

Add strict Pydantic models:

```python
class ManagerPlan(BaseModel):
    schema_version: Literal["1"]
    objective: str
    acceptance_criteria: list[str]
    steps: list[PlanStep]  # 1..8
    plan_revision: int

class PlanStep(BaseModel):
    step_seq: int
    kind: Literal[
        "specialist_dispatch",
        "advisory_tool",
        "clarification",
        "effect",
        "verification",
    ]
    specialist: Literal[
        "onboarding_conductor",
        "integration_agent",
        "sales_recovery_agent",
    ] | None
    situation: str
    desired_outcome: str
    acceptance_criteria: list[str]
    allowed_effect_classes: list[str]
```

Validation rules:

- Steps are sequential, unique and numbered from one.
- A specialist is required only for `specialist_dispatch`.
- Advisory steps cannot declare effects.
- Maximum eight active steps.
- Free text is redacted before persistence.
- Unknown specialist, effect class or step kind fails closed.

### Handoff protocol

Extend `SpecialistHandoff` with:

```python
task_id: UUID
step_id: UUID
plan_revision: int
attempt: int
acceptance_criteria: tuple[str, ...]
policy_ref: str | None
```

Replace the current return shape with:

```python
class SpecialistReturn(BaseModel):
    status: Literal["completed", "needs_owner_input", "blocked", "failed"]
    action_summary: str
    outcome_summary: str
    evidence_refs: list[EvidenceRef]
    effect_intents: list[EffectIntent]
    owner_question: str | None
    proposed_outcome: str | None
    reason_code: str | None
```

`needs_owner_input` requires `owner_question`. `blocked` requires `reason_code`. Effect intents are proposals only and never execute directly.

### Database migration

Use the migration allocator and make an additive migration.

Extend `manager_tasks`:

- Add `queued` to the status constraint.
- Add `plan_revision INTEGER NOT NULL DEFAULT 1`.
- Add nullable `terminal_outcome` constrained to:
  `completed_with_effect`, `completed_no_action`, `failed`, `escalated`, `cancelled`.
- Add `owner_notification_status NOT NULL DEFAULT 'not_required'`, constrained to:
  `not_required`, `pending`, `accepted`, `delivered`, `failed`.

Extend `manager_task_steps`:

- Add `plan_revision INTEGER NOT NULL DEFAULT 1`.
- Add nullable `specialist TEXT`, constrained to the three specialists.
- Add `superseded` to step statuses.
- Add `advisory_tool` to step kinds.
- Add `pipeline_step` to evidence kinds.

Update RLS, purge ordering, migration tests and task-store constants in the same change.

## 3. Ordered Implementation Packages

### Package 1: Runtime Scope and Tenant Isolation

- Split the existing roster into `SPECIALIST_ROSTER` and `ADVISORY_TOOL_REGISTRY`.
- `SPECIALIST_ROSTER` contains exactly the three Phase-1 specialists.
- Stop importing or dynamically registering the six future lane specifications.
- Remove future-lane spawn tools from the Manager model.
- Update the Manager prompt so those domains are described as advisory capabilities.
- Advisory tools may analyse, prepare and draft, but may not send, spend, commit, configure or mutate external state.
- Apply `resolve_lane_tenant` semantics to every Integration and Onboarding tool.
- The ambient dispatch tenant always wins. Model-supplied tenant identifiers are ignored.
- Add a build-time assertion that every tenant-scoped agent tool uses context-derived tenancy.
- Filter owner-visible connectors to Shopify and Google Sheets. Placeholder connectors, including Amazon, must be described as unavailable.

Acceptance:

- Runtime roster count is exactly three.
- No future-lane spawn tool exists.
- Cross-tenant UUID injection tests fail closed.
- “Connect Amazon” produces an honest unsupported response with no promised follow-up action.

### Package 2: Executable Plan Store

- Add typed `create_plan`, `load_plan`, `revise_plan`, `claim_next_step` and `complete_step` APIs around the existing task store.
- Create the task before the first specialist dispatch.
- Persist the complete plan and first current step atomically.
- Use source message SID as the task idempotency key.
- Admit one active objective-bearing task per tenant.
- Additional objectives become `queued`; direct FAQs remain available.
- Revisions never edit completed history. Mark pending old-revision steps `superseded`, increment `plan_revision`, and append replacement steps.
- Replace the existing route-triggered producer. It may temporarily remain as a compatibility adapter but must not create duplicate tasks.
- Record every plan, revision, step decision and terminal result in `tm_audit` using structured summaries, never chain-of-thought.

Acceptance:

- Duplicate inbound events create one task and one plan.
- A plan survives process restart and resumes at the same step.
- CAS prevents stale workers from advancing or regressing a task.
- A revised plan preserves prior step history.

### Package 3: Durable Manager Workflow

Add a DBOS workflow keyed by `task_id`:

```text
load task and plan
    ↓
claim current step
    ↓
validate capability, prerequisites and policy
    ↓
dispatch specialist or advisory tool
    ↓
consume structured result
    ↓
Manager review decision
    ├─ accept_step → persist evidence
    ├─ revise_step → append plan revision
    ├─ ask_owner → pending question + interrupt
    ├─ continue → claim next step
    ├─ complete → verify objective
    └─ escalate → incident + owner/VTR report
```

Manager turn handling:

1. Run opt-out, DSR, delivery and approval handlers first.
2. Capture the inbound conversation turn.
3. Load conversation history, business context, active task and pending question.
4. Produce structured triage:
   `direct_reply`, `answer_pending`, `new_task`, `task_status`, `cancel_task`.
5. Direct replies create no task.
6. `answer_pending` resumes the exact task and step.
7. `new_task` produces a validated `ManagerPlan`.
8. A side question is answered without losing the active task.
9. A cancellation terminates the task and cancels unexecuted effects.

Limits:

- Eight steps per plan.
- Two revisions per step.
- Six Manager/specialist cycles per workflow run.
- Existing cost, token, tool and wall-clock limits remain.
- Limit exhaustion produces `blocked` plus a VTR incident, never silence.

Graph changes:

- Remove specialist-to-END edges.
- Route every specialist to `manager_review`.
- Only `manager_review` may advance or terminate a task.
- Preserve the approval interrupt and campaign effect path.
- Add `TEAM_MANAGER_LOOP_MODE=legacy|shadow|enforce`.
- Default production to `legacy` until final promotion.

### Package 4: Onboarding Specialist

- Stop the active onboarding journey from consuming ordinary owner messages before the Manager.
- Preserve the journey tables as resumable specialist state.
- Convert existing journey operations into context-scoped tools:
  `read_onboarding_state`, `extract_owner_answer`, `record_answer`,
  `next_required_question`, `record_skip`, `apply_correction`,
  `profile_completion_check`, and `activation_check`.
- The specialist receives the latest owner reply in memory but stores only extracted, redacted fields in task state.
- It absorbs multi-field and out-of-order answers in one turn.
- It never asks for a field already present in the conversation, journey state or confirmed profile.
- It returns `needs_owner_input` with one question or `completed` with deterministic evidence.
- Profile completion and full activation remain deterministic and cannot be asserted by the model.
- Add a policy-confirmation stage that records the owner’s machine-enforceable action bounds. Missing policy remains deny-all.

Acceptance:

- Greeting, correction, skip, resume, volunteered information and multi-field answers work through the actual specialist node.
- Every test transcript proves an Onboarding spawn and specialist return.
- No test may count the legacy journey interceptor as Onboarding Agent evidence.

### Package 5: Integration Specialist

Expose only these context-scoped tools:

- `list_supported_connectors`
- `read_integration_state`
- `start_oauth`
- `check_oauth_status`
- `pull_sample`
- `propose_mapping`
- `confirm_mapping`
- `commit_ingestion`
- `schedule_recurring_pull`
- `verify_connector`

Shopify:

- Require and validate the shop domain.
- Use the existing single-use OAuth state.
- Use fixed canonical mapping.
- Pull, ingest, verify counts and schedule cadence server-side.

Google Sheets:

- Complete OAuth without manual credential pasting.
- Let the owner select the spreadsheet/tab through the supported UI flow.
- Pull a sample, run the existing mapping reasoner, ask for confirmation, commit ingestion and schedule cadence.
- Never expose raw customer rows to the model.

Both connectors:

- Persist every phase before replying.
- Resume from OAuth callback, owner reply or process restart.
- Return `needs_owner_input`, `blocked`, `failed` or evidence-backed `completed`.
- Configuration or connector failure must not be reported as missing owner input.
- Remove active field-mapping and non-Shopify placeholder stubs.

Acceptance:

- Real development canaries complete Shopify and Sheets from start through recurring-pull verification.
- OAuth replay, expiry, wrong tenant and callback duplication fail closed.
- Service restart at every phase resumes correctly.

### Package 6: Sales Recovery Specialist

- Preserve existing cohort detection, structured campaign plan, grounding validation, self-evaluation and continuation handling.
- Consume the Manager’s desired outcome and acceptance criteria.
- Return a typed `SpecialistReturn`.
- A proposed campaign becomes an `effect_intent`; it does not bypass Manager review.
- Manager review verifies cohort grounding, expected recovery, evidence references and requested outcome.
- Accepted effect intents pass through business policy, consent, opt-out, caps, ownership and autonomy gates.
- L2 pauses for approval. L3 may execute only inside owner policy and all deterministic rails.
- Record campaign and delivery evidence before completing the task.

Acceptance:

- Seeded cohort produces a grounded plan, approval or autonomous effect as appropriate, and a truthful owner report.
- Empty or insufficient data produces a useful recovery path, not a fabricated plan.
- Rejected, revised and failed plans return to the Manager.

### Package 7: Autonomy and VTR Control

- Keep the existing 20-clean-approval earning threshold and explicit owner opt-in for normal L2→L3 promotion.
- Add `force_l3` to the per-capability VTR override.
- `force_l3` requires verified VTR assignment, a scrubbed reason and an atomic audit record.
- VTR force promotion bypasses only the earning threshold and owner opt-in; it does not bypass policy or safety rails.
- Retain demote, revoke, freeze and unfreeze.
- Reuse tenant takeover/release as the tenant-wide emergency freeze.
- Tenant-wide freeze pauses dispatch and cancels every open effect batch atomically.
- Do not implement tenant-wide promotion.
- Update Ops UI to show earned versus VTR-forced provenance and the last transition.

## 4. Verification Programme

### Automated tests

- Full existing Python, migration, RLS, web, lint and static-access suites remain green.
- Add state-machine tests for every legal and illegal task/step transition.
- Add graph tests proving all three specialists return to Manager review.
- Add restart tests after plan persistence, dispatch, specialist return, owner question, OAuth callback, approval, effect execution and notification.
- Add concurrency tests for duplicate messages, simultaneous owner replies and stale workers.
- Add adversarial tests for foreign tenant IDs, prompt injection, unsupported connectors, fabricated completion and direct effect attempts.
- Add advisory-tool tests proving no spawn, mutation or execution claim.

### Server-side scenario pack

Run at least 120 synthetic scenarios:

- 40 Manager reasoning, context, topic-switching and task-management scenarios.
- 25 real Onboarding specialist scenarios.
- 25 Integration scenarios split across Shopify and Sheets.
- 30 Sales Recovery, approval, autonomy and rail scenarios.

Run the 30 critical scenarios three times. Requirements:

- All hard assertions pass.
- No silent terminal.
- No repeated in-context question.
- No cross-tenant access.
- No unsupported capability promise.
- No effect without the required policy and authority.
- Every specialist scenario contains audit evidence of spawn, return and Manager review.
- Transcript judge score is at least 4/5 in every dimension and mean at least 4.5.

### Durable evidence

Create a redacted evidence manifest containing:

- Source commit SHA and deployment SHA.
- Migration version.
- Feature-flag mode.
- Scenario result counts.
- Judge scores.
- Real canary references.
- Transcript hashes.
- Confirmation that synthetic tenants were removed.
- Confirmation that no unintended real sends occurred.

## 5. Rollout and Merge Order

1. Runtime roster and tenancy correction.
2. Schema migration and executable plan store.
3. Manager workflow in `shadow`.
4. Onboarding specialist conversion.
5. Integration specialist completion.
6. Sales Recovery return/review integration.
7. Autonomy and VTR controls.
8. Exhaustive test pack and evidence manifest.
9. Development `enforce`.
10. Designated Concierge tenant canary.
11. Exact-SHA production promotion.

Shadow acceptance requires at least 50 conversations with no safety divergence and documented comparison between legacy and planned decisions.

Production promotion requires every verification gate to pass. Rollback changes `TEAM_MANAGER_LOOP_MODE` to `legacy`; migrations remain additive and compatible.

## 6. Definition of Done

Phase 1 is complete only when a real owner objective can traverse:

```text
Owner message
→ contextual Manager understanding
→ durable plan
→ correct specialist
→ structured return
→ Manager review/replan
→ deterministic effect rails
→ verification evidence
→ truthful owner outcome
```

The task must survive retries and restarts, never ask for known information, never access another tenant, never claim unsupported work, and never execute outside policy or authority.
