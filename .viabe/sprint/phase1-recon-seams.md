# Phase-1 recon seams — file:line map for the remaining B-spine (accelerator for the next session)
*From 5 read-only recon passes, 2026-07-01. Pairs with the row map (`phase1-overnight-plan-and-questions.md`). All paths under `apps/team-orchestrator/`; migrations at repo root `migrations/`. dev @ e6f5472 green; migrations resume at 151 (149=VT-521, 150=VT-524).*

## Cross-cutting patterns to LIFT VERBATIM (don't reinvent)
- **RLS+FORCE+DSR**: `ENABLE + FORCE ROW LEVEL SECURITY` in the SAME migration as CREATE TABLE + 4 policies keyed `tenant_id = app_current_tenant()` (template: `migrations/147`, `125`, `124`). Register new tenant tables in `src/orchestrator/dsr_purge.py:_PURGE_ORDER` (children-before-parents, commented) **in the same PR** (VT-518/VT-524 lesson). Service writes go through `get_pool()` (RLS-bypassing) with explicit tenant_id.
- **CAS state machine (no terminal regress)**: `agents/coordinator.py:768 _write_status(..., expected_from=<tuple>)` — UPDATE `WHERE status = ANY(expected_from)`; stale writer no-ops (logged, not raised). Mirror for `manager_task_steps`.
- **Orphan reaper shape**: `orphan_reaper.py` (VT-481) — startup sweep, age-floor + terminal stamp + best-effort/never-raise, service-role, cross-tenant. New PREDICATE for B2 (has-active-step/wait, not just age).
- **Reviewer de-id views**: `migrations/134_vt377_vtr_assignment_scoping.sql` `app_vtr_operator()` GUC + `operator_assignments` (mig 072) predicate + operator-JWT `FOR SELECT` policy (mig 147:71-87). REUSE `operator_assignments` — 134's docstring warns against forking it ("the N1 lesson").

## VT-525 — B2 manager_tasks + manager_task_steps
- NET-NEW tables. Reconcile ABOVE (don't replace): `business_plan` (mig 124, roadmap), `agent_work_items` (mig 125, per-item dispatch — today's thinnest "task"), `pipeline_runs` (mig 005, execution evidence). Hierarchy `manager_task → task_step → pipeline_run/workflow/effect`.
- **Two parallel guarded pipelines exist** (no unified effects table): (1) campaign = `collapse.py`→`routing.py:route_after_collapse/after_approval`→`agent/tools/request_owner_approval.py`(interrupt)→`supervisor.py:_campaign_execute_node`→`campaign/execute.py`; (2) Gap-5 agent = `coordinator.py`→`sales_recovery_executor.py`→`agents/approval_glue.py`→`agents/customer_send.py:agent_send_draft` (the 7-gate chokepoint). Both share `pending_approvals` (mig 052/128, one-open-per-tenant partial-unique index).
- **DECIDED (my default):** `task_step` carries polymorphic `evidence_kind {campaign_plan|agent_work_item|pipeline_run}` + `evidence_ref` (by-value, no hard FK — matches today's inconsistent linkage). NO fold of either pipeline.
- version-mint lock pattern: `business_plan/store.py:write_new_version` (`SELECT id FROM tenants WHERE id=%s FOR UPDATE`). idempotency_key = per-tenant unique (mirror `agent_work_items_open` partial index).
- Migrations 151 (manager_tasks) + 152 (manager_task_steps) + reviewer views (fold or 153). Consider a status lookup table (more states than pipeline_runs, which already ALTERed its CHECK twice).

## VT-526 — B3 two-way delegation
- **DECIDED (my default):** thin orchestration layer over the EXISTING single-spawn primitive, driven by `manager_task_steps` order — do NOT do LangGraph graph surgery across the 9 roster lanes.
- `roster.py:86-231` — `SpecialistHandoff.situation` is hardcoded `""` (the "empty framing = contract-failure" gap is LIVE); `desired_outcome` = static `spec.default_outcome`. Fix = a manager-authored structured-output field on the orchestrator_agent reasoning step. `context_slice` already real (VT-466).
- `SpecialistReturn` is DEFINED + shape-tested (`test_roster_registry.py:180`) but the manager NEVER reads it (`RETURN_STATE_KEY` unread). Core net-new = a `route_after_collapse`-analog node, generic across 9 lanes, that reads the specialist's terminal state → accept/revise/next-specialist/clarify/escalate. Dispatch = `supervisor.py:build_supervisor_graph` + `handoffs.py:make_spawn_tool` (`Command(goto=…, graph=Command.PARENT)`).

## VT-527 — B4 clarification/continuity
- Generic pending-questions table NET-NEW (reuse `onboarding/journey.py` message_sid-idempotency + `pending_approvals` timeout_at/defer-expiry; DON'T touch `onboarding_journey` shape — it's singular-per-tenant, reset-on-restart). Resume = DBOS + LangGraph PostgresSaver (`thread_id=run_id`).
- Cross-task running-context (owner prefs / confirmed facts / recent decisions) NET-NEW — none today (`onboarding_journey.answers` is the only precedent, single-purpose).
- reviewer-vs-owner routing EXISTS: `owner_surface/vtr_classifier.py:classify_escalation_route`. **VTR live-resume INVERTS CL-426 async-VTR (`vtr_digest.py` is daily Telegram, no read-back) — Fazal question; my default = keep async.**

## VT-528 — B5 capability truth
- **DECIDED (my default):** capability registry = CODE (extend `agents/activation_registry.py` + `run_control/registry.py` frozen-dataclass + import-time-invariant precedent) → **0 migrations**; environment via `EXPECTED_ENV`, not a DB toggle. Effect-class taxonomy = the plan's {send|db-mutation|connector|campaign|advisory}.
- Verifier framework NET-NEW (deterministic-first, per `vtr_classifier`/`approval_reply` precedent — NOT an Opus call per function; `agent/self_evaluate.py` is draft-quality-only). Connector `health_check` NET-NEW on `connectors/base.py` (+ google_sheet/shopify). Ops Console already extensive (`api/ops_run_control.py` 1105L, `api/ops_vtr_console.py`) → EXTEND.

## VT-529 — B6 recovery/safety/ops
- Retry budgets: `backoff.py` (compute_delay, CircuitBreaker) has NO live caller — wire it. Dead-letter = Razorpay-only (`billing/dead_letter.py`, mig 114) — generalize. Freeze GAPS: no global customer-SEND kill (only coordinator-loop env + test mock), no per-function, no per-campaign. Reviewer TAKEOVER absent. No-orphan: runs/approvals/sends covered; TASKS/WORKFLOWS/CAMPAIGNS not. Alerts: `outbound_failure` declared-but-never-fired; `silent_terminal`/`orphaned_task`/`rail_bypass` absent (`alerts/triggers.py`).

## VT-530 — C2a self-handling
- **0 migrations.** Add `recovery_attempted` `event_kind` (free TEXT, advisory vocab `tm_audit.py:60`) + link via existing `parent_audit_id` self-FK. Instrument at `agent/orchestrator_agent.py:299-352 _tool_error_to_tool_result` (VT-484 — the implicit self-handling seam today). Deterministic routers stay authoritative (`error_router.py`/`strategies.py`/`escalation.py`); C2a captures the manager's reasoning for novel/unclassified cases.

## VT-531 — C3 correction store
- 1 migration (mirror mig 147). Table keyed FK→`tm_audit_log.id` + tenant_id + run_id + correction-kind (REUSE `autonomy.RegressionKind`) + REDACTED text (via `pii_redactor.redact`) + provenance/authority + retrieval-gate placeholder cols default-closed (`retrieval_eligible bool default false`, `expires_at`, `authority`). **HARD SEQUENCING: capture in `approval_glue.apply_agent_decision` at needs_changes/rejected/timeout BEFORE `outbox_redaction.redact_batch_close()` sha256s the owner_feedback text** — same txn. Add to `_PURGE_ORDER` same PR. Retrieval OFF (capture-only). owner_feedback (mig 041 thumbs) stays separate.

## VT-531 C3 — SEEDABLE MEMORY added (Fazal 2026-07-01 answer)
C3 now ALSO builds the **seedable-memory mechanism + seed-then-learn-beyond posture** (CL-2026-07-01-no-fixed-playbook). Mutable SEED MEMORY (archetype/business knowledge as CL-426 accelerant, agent overwrites through learning) — NOT fixed notes. Default = seed, not empty. Seed CONTENT is a separate Fazal/archetype follow-up (not blocking the mechanism). The correction store (above) is the learning-capture half; the seedable memory is the head-start half. Both feed the same learnable memory the specialist reasons from.

## VT-532 Track D — RESHAPED: no fixed playbook (Fazal 2026-07-01 answer)
KILL the ~100-note corpus + the 69-note SR retrofit. NO static retrieval corpus, NO authored notes. Knowledge = LLM reasoning + C3 memory-loop. Build: (1) the **no-fabricated-numbers output rail** (claim grounded-or-hedged — the ONE kept guardrail, consistent with C1); (2) a **held-out advice-quality EVAL** (factuality/actionability/relevance/tone) as MEASUREMENT-before-graduation only — never authors/scripts/confines advice, not a corpus. Cold-start = concierge (VTR reviews; corrections feed memory). Ledger: CL-2026-07-01-no-fixed-playbook (supersedes sr-playbook-bar). `l4_documents`/`l4_corpus` playbook-retrieval path (if any wired to SR) is now dead scope — do not feed it into specialist context.

## OC1 (with B5/B6) — owner-policy enforcement is OFF today
`agents/business_policy.py:assert_within_policy` (mig 144 `tenant_business_policy`) exists but `assert_customer_send_allowed(enforce_policy=…)` defaults False and NO live caller passes True → segment/freq/spend bounds NOT enforced on real sends. Wire it into the customer-send path. 0 migration.
