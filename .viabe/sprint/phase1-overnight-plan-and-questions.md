# Phase-1 LOCKED overnight build — row→work map + CONSOLIDATED QUESTIONS
*CC, 2026-07-01, under Cowork 20260701T200000Z (Fazal: plan locked, build autonomously, ask everything up-front). Plan = `docs/clau/phase1-plan.md` (LOCKED). Recon = 5 read-only Explore passes over the whole repo.*

HEAD at plan: **2a72ed3** (VT-520 landed on origin/dev). VT-IDs **VT-521..533** allocated up-front. Migration numbers allocated per-row **at serial build** (builds are serial on the one shared tree — no parallel build phase — so the CL-424 anti-race is satisfied; intended migrations listed per row by purpose). Next free migration = **149**.

---

## ⛔ BLOCKING QUESTIONS — answer before I can COMPLETE the tagged row (I build everything else meanwhile)

**Q1 — Sales Recovery playbook source (Track D / VT-532).** The ~100-sourced-notes bar: the corpus already holds **69 Fazal-authored operating notes** (authorship, not external citation) + `l4_documents` + retrieval already wired into the SR specialist. Missing = provenance/sourced_number/review_date columns + a gold-set eval harness. **Where do "sourced plays" come from — (a) retrofit the 69 as `provenance=fazal-operating-playbook` (ships tonight, zero new authoring) or (b) autonomous web-research of externally-cited plays with sourced numbers?** Is autonomous web-research curation of sourced numbers allowed at all? The plan BANS invented numbers, so a scraped stat needs a real citable source or it's worse than none. **My default if unanswered:** build the schema + gold-set harness + retrofit the 69 as fazal-provenance tonight; treat reach-to-100-with-external-plays as a content follow-up gated on your call. I will NOT fabricate sourced statistics.

**Q2 — Send allowlist for the real-delivery canary.** Task says "allowlist = Fazal's 3 numbers; all else mocked." I never fabricate a number. **What are the 3 allowlist numbers (or is `DEV_SEND_ALLOWLIST` already set with them)?** Only +919321553267 is known to me. BLOCKING for any real send/delivery-callback canary (B1 proof / VT-524); every non-send row proceeds without it.

**Q3 — DSR subject scope (Track A / VT-523).** Access/correction subject = **tenant/owner-only** (matches 100% of existing purge/export code) or also **per-end-customer** (a name inside `customers`)? Default: tenant-only. Also confirm the DPDP "access & correction" (VT-523) and the C3 learning "correction store" (VT-531) stay SEPARATE systems (they both got named "correction") — default: separate.

## 🕓 HUMAN / THIRD-PARTY GATES — queued, never faked/stubbed past; I build the code, these gate the canary/launch
- **Q4 Meta WABA Embedded Signup** (real merchant-owned WABA, dedicated number) — gates real WhatsApp prod paths. Also `team_welcome3` UTILITY approval is pending Meta (async); your signup re-run verifies delivery once approved.
- **Q5 Shopify + Google merchant OAuth** (real accounts) — gates connector reads + their canaries.
- **Q6 Razorpay** subscription+webhook — NOT admission-blocking (30-day trial); code built now, live canary before day-30 conversion.
- **Q7 Counsel/DPDP review + Grievance Officer appointment** — gates admitting the design partner (non-waivable privacy). I build access/correction; the appointment + counsel sign-off are yours.
- **Q8 Mumbai prod DB + prod authorization** (VT-231, CL-431) — ALL overnight work lands on DEV; prod is Fazal-only.
- **Q9 VTR review SLA + per-reviewer capacity NUMBERS** — I build the mechanism with configurable thresholds; the numbers are yours (before launch). Give them, or I ship placeholders you set later.
- **Q10 C4 graduation thresholds** — not overnight-blocking (post-soak); yours before first graduation.
- **Q11 Ownership-migration evidence** — existing tenants → verified ONLY from documented VTR evidence; no auto-backfill. Needs your evidence.
- **Q12 `main` promotions** (Pillar-7) — every dev→main is yours; I never touch main.

## ✅ ASSUME-AND-PROCEED (I build on these defaults tonight; correct me and I'll adjust)
- **D1** B1 terminal enum `{completed_with_effect, completed_no_action, failed, escalated, cancelled}` lands on `pipeline_runs.status` NOW (with effect-split backfill); B2 `manager_tasks` reconciles later, not a rewrite.
- **D2** Keep the SINGLE hardened Twilio inbound webhook; add a real dispatch rule for delivered/read/failed/undelivered → new notification-state (no separate Twilio URL / console change).
- **D3** `manager_task : pipeline_run = 1:N`; keep `task_step:pipeline_run` 1:1; leave the proven interrupt/resume seam (`thread_id=run_id`) untouched.
- **D4** Manager "accept" is a `manager_task_steps` transition, NOT a second gate — effect-gate entry stays inside the specialist/executor (lowest risk to the proven non-bypassable rails).
- **D5** Capability registry = CODE (extend `activation_registry` pattern), not a DB table (matches 3 existing precedents).
- **D6** Evidence-refs = typed pointers into `tm_audit_log`/`pipeline_runs`/`pending_approvals`; reuse `tm_audit_log` first, dedicated evidence table only if read-back/rollback outgrows JSONB.
- **D7** C2a reasoning sits ABOVE the deterministic `error_router` (router stays authoritative for the 10 typed failures; the manager reasons only for novel/unclassified situations + how-to-retry within a chosen strategy).
- **D8** Global freeze = new `env_config` key checked at webhook-pipeline + campaign-execute; campaign freeze = a `paused` state on `campaigns.status`; keep L3 `AGENT_AUTONOMY_GLOBAL_FREEZE` as-is.
- **D9** C3 correction store keyed to `tm_audit_log` decision + approval/batch id; reuse `operator_assignments`+`app_vtr_operator()` for the reviewer view; retrieval stays behind a kill-switch flag (capture-ONLY tonight — a reviewer mistake must never silently become policy).
- **D10** The 2 pre-existing pre-push reds get FIXED FIRST (VT-521, VT-522) so dev pushes are green. Fail-closed: I fix the `tm_audit_log` emit to not persist a dangling `run_id`, and seed `onboarded` in the SR-executor fixtures ONLY after confirming (from VT-517's design) the onboarded gate is intended — I never bend a correctness gate to make a test pass.

---

## ROW → WORK MAP (execution order; serial builds on the shared tree; Track A + D interleave where independent)

**Unblock (first — so every subsequent dev push is green):**
- **VT-521** — Fix VT-514 `tm_audit_log_run_id_fkey` violation (audit emit writes a `run_id` absent from `pipeline_runs`). Emit-guard/test fix; migration only if an FK-nullability change is required.
- **VT-522** — Fix VT-517 SR-executor gate-order (`skipped_not_onboarded` fires before `skipped_no_candidates`). Confirm gate intended → seed `onboarded` in fixtures; never bend the gate.

**Track A (code; parallel-independent):**
- **VT-520** — welcome UTILITY (`team_welcome3`) — LANDED (2a72ed3).
- **VT-523** — DSR data-principal ACCESS (ticket-track the existing VT-341 export via `dsr_tickets`) + CORRECTION flow (intake → record field/value → operator/agent UPDATE → ticket lifecycle) + DSR canaries on deployed dev (access, correction, erasure of `tm_audit_log`/`debug_events` — extend `vt185_dsr_purge.py`, outbound-message redaction). Migration: DSR correction substrate.

**B-spine (given order):**
- **VT-524 — B1** truthful terminal + owner notification: 5-value terminal enum + effect-split; `owner_notification_status` + `communication_status` (new linked table); wire the status-callback → state writer (today delivered/read are discarded, failed hits a stub); the owner-notification escalation ladder EXECUTOR (wire the unwired `failures`/`backoff`/`strategies`/`escalations` inline → owned incident, never silence); `silent_terminal` + `outbound_failure` alert detectors. Migrations: owner_notifications + terminal enum + owner-contact incident.
- **VT-525 — B2** canonical `manager_tasks` + `manager_task_steps`: CAS `version`, transition-guard state machine (reuse `transitions.py`), idempotency key, redacted objective + source-msg-ref + hashes (raw prose dropped), evidence-refs, policy-ref; orphan detector (per-status thresholds); DSR-purge `_PURGE_ORDER` registration + RLS+FORCE in the same migration; linkage columns on pipeline_runs/pending_approvals/campaigns/agent_work_items; de-identified `app_vtr_operator()`-scoped VTR views. Migrations: tables / linkage / VTR views.
- **VT-526 — B3** two-way delegation: a real manager-decision node consuming an extended `SpecialistReturn` (accept/revise/next-specialist/clarify/escalate), replacing today's dead-end-at-END; contract-failure guard rejecting empty `situation`/`desired_outcome`/static-default framing (violated today); accepted effect → the EXISTING guarded pipeline (collapse→approval→execution), never a bypass; persisted ordered multi-specialist step plan on `manager_task_steps`. No new migration.
- **VT-527 — B4** clarification + continuity: task-scoped question/wait queue (≤3/turn, expiry, correlate tenant+task+msg-ref+active-wait; generalize `pending_clarifications`); running-context store (task_summary/confirmed_facts/open_questions/owner_prefs/recent_decisions; read-before-ask); general-path durable resume; wire `vtr_classifier` into the live path; give the orchestrator agent a real clarifying-question tool (`escalate_to_fazal` is a STUB today). Migrations: clarification queue + running-context.
- **VT-528 — B5** capability truth: code capability registry (live/advisory/disabled × tenant+env, prereqs, effect class, policy rail, verifier, rollback, env) extending `activation_registry`; typed per-effect evidence contracts (DB write+read-back, connector health-read, advisory grounded-delivered — send/campaign largely met); independent per-function verifier (not self-grade); Ops Console task/evidence/takeover surface (extend run-control). Migration: Ops takeover state (if needed).
- **VT-529 — B6** recovery/safety/ops: generic cross-entity no-orphan invariant (tasks/runs/workflows/approvals/sends); deterministic retry budgets + dead-letter + operator redrive (generalize beyond Razorpay); TRUE global freeze (`env_config` key at webhook-pipeline + campaign-execute) + campaign freeze state; exactly-once at model-call/handoff boundaries; `orphaned_task` + `rail_bypass_attempt` alert detectors. Migrations: dead-letter/retry-ledger + freeze states.
- **VT-530 — C2a** self-handling/recovery (ships with B3/B6): manager reasons a recovery (retry-differently / alt-path / re-plan) BEFORE escalating; `recovery_attempted` audit `event_kind`. Migration: event_kind enum ALTER (small).
- **VT-531 — C3** learning-capture substrate (build now, runs during soak; retrieval OFF): outcome/correction store keyed to `tm_audit_log` decision + approval/batch; STRUCTURED correction fields (no raw PII); downstream-outcome writer; provenance/authority/expiry/contradiction columns reserved for later retrieval-gating; RLS+FORCE + DSR-reg in-migration + `app_vtr_operator()` view; writer call-sites (approval_glue, ops_resolve, self_evaluate). Migration: correction_outcomes.

**Track D (code; parallel-independent):**
- **VT-532 — Track D** advice quality: `l4_documents` provenance columns (rationale/sourced_number/applicability_exception/provenance/review_date) + retrofit the 69 notes; gold-set eval table (physically disjoint from the retrieval corpus) + a factuality/actionability/relevance/tone grader + a pass-bar gate. Content-to-100 gated on Q1. Migrations: l4 ALTER + gold-set table.

**Launch-foundation contracts (with B4–B6):**
- **VT-533 — Reviewer (VTR) + Owner-policy contracts:** VTR review SLA + per-reviewer capacity (mechanism now, numbers per Q9), queue ownership, absence coverage (queue-or-halt, NEVER auto-proceed), audit trail, second-reviewer precondition (assignment scoping — mostly exists); owner-policy versioned grants (segments/actions/frequency/spend/expiry/revocation/precedence) checked on every effect (much rides `tenant_business_policy` mig 144). Migration: VTR SLA/capacity + owner-policy versioning (as needed).
