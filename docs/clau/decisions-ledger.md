# Clau Decisions Ledger — Standing decisions only

Source: `docs/clau/entries/CL-*.md` (per-entry files migrated from Notion 2026-05-25).
This file is regenerated when new Standing entries appear; do not edit by hand.

**Last reconciliation:** 2026-05-25 ~12:50 IST — Cowork applied Clau's review (6 supersessions marked, 4 missing Standing entries added, CL-385/386 deduped). Every line verified against its source `entries/CL-*.md` file per Rule #14.

---

- **CL-1** (2026-05-12) — Tooling decision: stay on Notion for project management; defer Linear/GitHub Issues migration *[NOTE: superseded 2026-05-25 by the .viabe/sprint/ + docs/clau/ migration — Notion now read-only archive]*
- **CL-2** (2026-05-12) — Deployment shape locked: sibling repos + shared accounts + separate projects within each account
- **CL-4** (2026-05-12) — Launch plan: Reports June 15, Team soft launch July 15 (invite-only 10 design partners), Team full launch
- **CL-5** (2026-05-12) — Meta WhatsApp templates tiered: Tier-A (5 launch-blocking) + Tier-B (17 concierge-until-approved)
- **CL-6** (2026-05-12) — Implementation team is agentic, not human: CoderC (Claude Code orchestrator) + CoderX (external code reviewer)
- **CL-7** (2026-05-12) — Fazal-owned subtask count is 7 (VT-15, VT-16, VT-109, VT-111, VT-112, VT-113, VT-114, VT-115) + 13 vendor approvals
- **CL-11** (2026-05-12) — Meta template count grew 8 → 22 across batches; full list documented in VT-108
- **CL-13** (2026-05-12) — Notion edit pathways: update_content (search-and-replace) WORKS; replace_content + update_properties have quirks
- **CL-14** (2026-05-12) — Three-layer memory system live: project instructions (Layer 1) + Clau_Session_Log (Layer 2, this DB) + Resurrection File (Layer 3)
- **CL-16** (2026-05-12) — Fazal's communication preferences are STANDING and apply every session
- **CL-18** (2026-05-12) — Correction to prior log: update_properties WORKS for single-page targeted updates; only BULK updates have quirks
- **CL-19** (2026-05-12) — Step records use typed envelopes (declared input/output fields per step) not full-row snapshots
- **CL-20** (2026-05-12) — PII discipline: phone numbers tokenized, rest plaintext for debuggability **[SUPERSEDED — see privacy-decision cluster CL-385/CL-389/CL-390; Voyage-receives-raw-bodies is now LOCKED consent-gated]**
- **CL-21** (2026-05-12) — Step record retention 90 days then aggregated **[SUPERSEDED — see privacy-decision cluster / CL-390; retention now governed by LOCKED standing privacy decisions]**
- **CL-22** (2026-05-12) — Ops UI is Phase 1 launch-blocking; new VT parent VT-OpsConsole to be created with subtasks
- **CL-23** (2026-05-12) — Mermaid diagram styling pattern: stroke-width:2px mandatory, explicit color:#000, lighter fills with dark text
- **CL-24** (2026-05-12) — Orchestrator-as-agent locked: Opus 4.7 brain + own memory + spawns specialists; supersedes prior thin-router framing
- **CL-25** (2026-05-12) — Two-stage event filtering: deterministic pre-filter then orchestrator brain
- **CL-26** (2026-05-12) — L0 memory tier added: workspace-level operational memory for the orchestrator-agent
- **CL-28** (2026-05-12) — K-anonymity reverted to k=10 per concept doc Section 10; my May 4 k=5 decision was a Type 3 violation
- **CL-29** (2026-05-12) — Orchestrator triggering: event-driven, plugin-mediated. NOT continuous-loop observer.
- **CL-30** (2026-05-12) — Concept diagram shows connected systems including Phase 3+ greyed for vision context
- **CL-31** (2026-05-12) — Two concept diagram versions: (A) architect-facing, (B) investor/customer-facing
- **CL-32** (2026-05-12) — LangGraph supervisor library (langgraph_supervisor) chosen **[SUPERSEDED by CL-175]**
- **CL-33** (2026-05-12) — Phase 1 durability posture: accept LangGraph checkpointer-only gap **[SUPERSEDED by CL-35 / CL-36]**
- **CL-34** (2026-05-12) — Architecture diagrams on a separate Notion page from concept diagrams (different audience)
- **CL-35** (2026-05-12) — REVERSE of CL-33: Phase 1 will NOT ship with checkpointer-only durability; durable execution infrastructure chosen and implemented from Day 1
- **CL-36** (2026-05-12) — DBOS chosen over Temporal for durable execution substrate; Phase 1 ships with DBOS
- **CL-37** (2026-05-12) — Tonight's pre-execution cleanup complete; Week 1 execution plan published; tomorrow is Day 1
- **CL-41** (2026-05-12) — Three-repo architecture (viabe-reports + viabe-team + viabe-marketing); marketing repo deferred
- **CL-43** (2026-05-15) — Correction: DLT for Vodafone Idea is VILPOWER (vilpower.in), not 'Smartping' — Smartping is Videocon's
- **CL-44** (2026-05-15) — Vilpower DLT entity registration SUBMITTED: VI-1100095152 (RKecom Services OPC Pvt Ltd, ₹5,920.89 paid)
- **CL-46** (2026-05-15) — viabe-team repo created (github.com/rkecom-in/viabe-team); viabe → viabe-reports rename complete
- **CL-48** (2026-05-15) — VT-17 repo bootstrap COMPLETE — PR #1 green, 55 files scaffolded, branch protection active
- **CL-49** (2026-05-15) — GitHub Pro upgraded for rkecom-in (~$4/mo) to enable branch protection on private repos
- **CL-50** (2026-05-16) — Twilio account reused from Reports (single account, sub-accounts not created). TEAM_TWILIO_* env vars
- **CL-52** (2026-05-16) — Migrations applied via Path A: Claude Code uses TEAM_SUPABASE_SECRET_KEY from .env.local (dev-only)
- **CL-55** (2026-05-16) — L1 Knowledge Graph drops Apache AGE → Postgres + pgvector + time-aware relational
- **CL-56** (2026-05-16) — LangSmith replaced by Pydantic Logfire — aligns with DBOS OTel emission, predictable pricing
- **CL-57** (2026-05-16) — Memory layer: Mem0 OSS Python library for L1-L3 substrate **[SUPERSEDED by CL-324: L1 hand-built; L2/L3 Mem0 candidate deferred post-launch]**
- **CL-58** (2026-05-16) — uv and Ruff retained — OpenAI ownership accepted; tools MIT-licensed
- **CL-59** (2026-05-16) — Next.js upgraded 15 → 16 NOW before more code is written — scaffold is minimal, upgrade cost is hours
- **CL-67** (2026-05-17) — Dev testing architecture decided: 3-tier (CI / synthetic webhook fixtures / live Twilio sandbox)
- **CL-68** (2026-05-17) — VT-3.2 shipped: PR #8 merged. SubscriberState TypedDict + 21-transition machine + 4 invariants live
- **CL-69** (2026-05-17) — Post-VT-3.3a validation plan: 3-layer (fresh-session audit, live execution, AI code review)
- **CL-71** (2026-05-17) — Correction: 6th brief oversight — VT-3.3b assigned tenant lookup + rate limiting to team-web wrongly
- **CL-80** (2026-05-18) — Notion-vs-shipped drift report (VT-19/20/24/25/26/31)
- **CL-81** (2026-05-18) — DECISION: schema migrations are path-first (orchestrator-needs-first), not canonical-8-upfront
- **CL-82** (2026-05-18) — DECISION: RLS canonical mechanism is current_setting('app.current_tenant_id') GUC, not auth.jwt()
- **CL-88** (2026-05-18) — CORRECTION to CL-79: dual RLS mechanism (GUC for backend, JWT for client direct reads)
- **CL-97** (2026-05-17) — Env-rename PR ritual: ALWAYS pre-merge double-set; never atomic-swap
- **CL-98** (2026-05-18) — DECISION: env-rename PRs use pre-merge double-set ritual, never atomic-swap
- **CL-106** (2026-05-18) — Correction: VT-3.3c template list updated to match actual registered WhatsApp templates
- **CL-107** (2026-05-18) — Decision: error/failure handlers do not message the founder. Internal logging only
- **CL-118** (2026-05-18) — STANDING: Claude Code briefs delivered as single copyable fenced block, not split-prose
- **CL-127** (2026-05-18) — CORRECTION to VT-CI-fix-2 brief: used Python regex syntax in a POSIX ERE shell gate
- **CL-130** (2026-05-18) — CORRECTION: VT-3.4 (VT-27) spec uses outdated langgraph-supervisor API kwarg
- **CL-132** (2026-05-18) — STANDING: all VT-* PRs target main
- **CL-133** (2026-05-18) — Orchestrator-agent system prompt only describes behaviors actually wired in the current PR
- **CL-137** (2026-05-18) — STANDING: Phase 1 codebase must not run on deprecated APIs, EOL packages, or meaningfully outdated tools
- **CL-175** (2026-05-19) — Decision: Drop langgraph_supervisor library. Use langgraph.types.Command directly for handoff. Supersedes CL-26, CL-32, CL-136
- **CL-177** (2026-05-19) — CampaignPlan v0.1 contract locked: 7 fields, 5-state status enum **[SUPERSEDED — CampaignPlan v1.0 (discriminated union) landed via VT-37 / VT-122 / VT-33; sole contract on main per CL-260 snapshot]**
- **CL-191** (2026-05-18) — VT-3.4 PR 2/3 scope locked: VT-34 bundle contract + safe-empty L1/L2 fallbacks
- **CL-198** (2026-05-18) — VT-3.4 PR 2/3 brief inline-rescope resolved: branch rename, collapse-path deferred to PR 3/3
- **CL-205** (2026-05-19) — STANDING (operational, load-bearing at session start): recalibration codification
- **CL-213** (2026-05-19) — STATE SNAPSHOT: 2026-05-19 (long-running, final block) session close
- **CL-216** (2026-05-19) — TECH DEBT: test_dbos_step_resume.py committed with _wait_for_probe(timeout=60)
- **CL-217** (2026-05-19) — TECH DEBT: test_dbos_step_resume.py test driver uses subprocess.Popen without stderr capture
- **CL-220** (2026-05-20) — STANDING DISCIPLINE: every brief whose verification step depends on CI running must first verify the gate exists
- **CL-229** (2026-05-20) — State Snapshot template locked: 5 fixed fields (Critical Path / In Flight / Blocked On / Next Action / Do Not)
- **CL-235** (2026-05-20) — CORRECTION: collapse path persists CampaignPlan + SubscriberState activity fields only — NO apply_transition
- **CL-240** (2026-05-20) — DECISION: VT-29 scoped to wrap VT-3.3 webhook only; VT-3.5 scheduled-trigger wiring reassigned
- **CL-244** (2026-05-20) — CORRECTION: hard-limit-enforcement subtask is Notion Task ID VT-35; SDK skeleton is Task ID VT-32
- **CL-248** (2026-05-20) — DECISION: test-phase model split. claude-haiku-4-5 is the test/canary model; claude-opus-4-7 is production
- **CL-249** (2026-05-20) — DECISION: sales-recovery agent built on the anthropic Messages SDK (pure Python, already a dependency)
- **CL-252** (2026-05-20) — DECISION: admin-bypass merge (gh pr merge --admin) is the standing merge method for VT- PRs in Phase 1
- **CL-259** (2026-05-20) — TECH DEBT: VT-122 (PR #33) reconciled the campaigns table to CampaignPlan v1.0 via plan_json JSONB column
- **CL-260** (2026-05-20) — **CampaignPlan v1.0 is the SOLE CONTRACT on main; v0.1 retired.** VT-37 + VT-122 + VT-33 shipped this session. PR #34 (VT-33 system prompt v1.0) — 11 CI checks green. *(Added 2026-05-25 by Cowork per Clau review: marks the de-facto Standing decision distributed across VT-37/VT-122/VT-33.)*
- **CL-265** (2026-05-21) — DECISION: VT-50 tool return type conforms to VT-36's lean on-main SelfEvaluator Protocol
- **CL-266** (2026-05-20) — VT-50 blocked on VT-5.1: Path 1 (brief VT-5.1 first). + Dependency-chain ground-truth pass approved
- **CL-267** (2026-05-21) — DECISION: Canonical Sprint 2 SR-agent sequence (ground-truth pass). VT-4 is 6/8 done; VT-135 is a sibling
- **CL-268** (2026-05-20) — DECISION (Type 2): draft_message_variants is DEFERRED — not in v1. v1 LLM-backed tool set stays at 2
- **CL-269** (2026-05-21) — DECISION (Type 2, Fazal): draft_message_variants DEFERRED to Phase 1.5
- **CL-274** (2026-05-21) — DECISION (test strategy): two-mode canary pattern for real-API tests of LLM-backed tools
- **CL-278** (2026-05-21) — DECISION (Fazal): self-evaluate gate REVISE contract — all-reasons verdict, exactly one retry carry
- **CL-281** (2026-05-21) — DECISION (Fazal): Item 4 — fold verdict-model widening into the wiring subtask (Option 2)
- **CL-284** (2026-05-21) — DECISION (Fazal): dispatch-switch subtask scope locked — closure swap + supervisor.py v0.1->v1.0 path
- **CL-307** (2026-05-22) — STATE SNAPSHOT 2026-05-22 — PR #42 wire-through merged; VT-4 blocked on ingestion/profile substrates **[SUPERSEDED by CL-309]**
- **CL-309** (2026-05-22) — STATE SNAPSHOT 2026-05-22 (REVISED, supersedes CL-307) **[SUPERSEDED by CL-317]**
- **CL-317** (2026-05-22) — STATE SNAPSHOT — 2026-05-22 (session end) **[SUPERSEDED by CL-325 / CL-375 / CL-391 / CL-394 / CL-407 chain]**
- **CL-322** (2026-05-22) — DISCIPLINE RULE #12: verify row BODIES not just titles before escalating
- **CL-324** (2026-05-22) — DECISION (Type 2, final) + DISCIPLINE RULE #13: Memory substrate split — L1 hand-built; L2/L3 Mem0 candidate deferred. Stack decision not done until materialized. Supersedes CL-57
- **CL-325** (2026-05-22) — STATE SNAPSHOT 2026-05-22 (rev): memory-substrate decided **[SUPERSEDED by CL-375 / CL-391 / CL-394 / CL-407]**
- **CL-330** (2026-05-22) — **OWNER_INPUTS STRUCTURED-INTENT CORRECTION (Fazal, Type 3, supersedes UUID …8180):** owner_inputs stores STRUCTURED INTENT (not raw bodies); Twilio Body-drop preserved; lifetime-of-relationship retention (no 90-day timer); privacy notice line pending Fazal sign-off; Meta-terms pre-flight check mandatory. *(Added 2026-05-25 by Cowork per Clau review: THE load-bearing critical-path decision.)*
- **CL-342** (2026-05-22) — DECISION: Row A resolved — owner_inputs LLM-transmission is permitted under both Meta and Anthropic terms (primary-source verified)
- **CL-372** (2026-05-23) — CORRECTION: 'owner_inputs BUILD proceeds in parallel' was wrong — the build is HELD on Fazal-owned items
- **CL-374** (2026-05-23) — DECISION: Three compliance items CLOSED — Anthropic DPA (done), Twilio/WhatsApp terms verified, ZDR deferred
- **CL-375** (2026-05-23) — STATE SNAPSHOT 2026-05-23 (session close): compliance items all closed; owner_inputs unblock path in scope **[SUPERSEDED by CL-391 / CL-394 / CL-407]**
- **CL-376** (2026-05-23) — MILESTONE: VT-146 owner_inputs extraction-writer code merged to main behind disabled feature flag (PR #47 + #48)
- **CL-386** (2026-05-23) — **DISCIPLINE RULE #14 (Fazal-approved, in force 2026-05-23):** any closeout tracker / status summary / merge table / handoff must be reconciled against ground truth (`gh pr list --state merged` + log files) before trusted or relayed. Applies to Clau's own summaries. *(Dedupes CL-385 — same decision, same date; CL-385 is the long-form version with trigger detail.)*
- **CL-389** (2026-05-23) — **CORRECTION (framing): the privacy notice is a SYSTEM-LEVEL / product launch-gate** deliverable covering all of Viabe's customer-data handling (Orchestrator, Sales Recovery Agent, Composer, pipeline retention, DBOS hold, Anthropic/Voyage transmission, owner_inputs). NOT a sub-task of owner_inputs. *(Added 2026-05-25 by Cowork per Clau review.)*
- **CL-390** (2026-05-23) — DECISION (Fazal, LOCKED/STANDING): (1) Anthropic + Twilio + Voyage + DBOS-hold MANDATORY consent-gated exchanges, baked into privacy policy. (2) Voyage receives raw bodies. (3) owner_inputs ON for July (verify-first). (4) Privacy notice is system-level. These are STANDING; do not re-litigate.
- **CL-391** (2026-05-23) — STATE SNAPSHOT 2026-05-23 (session 3 close): Privacy/process excursion COMPLETE; L1 KG closed; owner_inputs-unblock path done (#50/#51 merged) **[SUPERSEDED by CL-394 / CL-407]**
- **CL-394** (2026-05-23) — STATE SNAPSHOT 2026-05-23 ~19:35: supersedes the 18:59 snapshot and ALL earlier ones. NOTE: log CL-numbering is unreliable (parallel writers); reference by Notion page-ID. **[SUPERSEDED by CL-407]**
- **CL-407** (2026-05-24) — **LATEST STANDING STATE SNAPSHOT** — 2026-05-24 session close. VT-4 ship-thin SHIPPED (PR #52 merged). owner_inputs verification is NEXT. Compressed 5-field form lives at `docs/clau/latest-snapshot.md`. *(Added 2026-05-25 by Cowork per Clau review: the current Standing anchor.)*
- **DR-15** (2026-05-25, Fazal-issued, STANDING) — **CANARY MANDATORY** + must hit real API + must verify the API returns expected information correctly + must fail (not skip) on any error. Cowork's plan-review checks for canary step explicitly; APPROVED without canary is a discipline violation. Vendor approvals (LangSmith billing, Twilio DLT, etc.) get pulled into brief-time dependencies rather than treated as post-launch. Full text at `docs/clau/discipline-rules.md` §Rule #15. Triggered by Cowork shipping VT-101 / PR #56 with mocks-only test coverage and no canary; Fazal's directive made it Standing immediately.
- **DR-16** (2026-05-26, Fazal-issued, STANDING) — **PRE-BRIEF-READY ACTIVE-CONTEXT CHECK MANDATORY.** Before dispatching any `brief-ready` signal, Cowork MUST run `python3 scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md` and add `cl_decisions_checked: [CL-N, ...]` frontmatter to the signal listing every row surfaced. Claude Code bounces brief-ready signals missing the field. Substrate file `docs/clau/active-context-summary.md` is Cowork's working digest of every active CL entry + sprint-brief contract; Cowork updates it on every important decision / change / merge / Fazal directive — failure to update on a material change is itself a rule violation. Full text at `docs/clau/discipline-rules.md` §Rule #16. Triggered by Cowork shipping VT-101 / VT-102 / VT-103 / VT-104 against CL-56 (LangSmith→Pydantic Logfire, Standing 2026-05-16) — Cowork had file-access to the decision and didn't read it; Fazal's directive made the mechanical check Standing immediately.
- **CL-416** (2026-05-26, Fazal-issued, LOCKED/STANDING) — **PIPELINE-OBSERVABILITY RETENTION = LIFETIME-OF-RELATIONSHIP.** For `pipeline_runs`, `pipeline_steps`, `phone_token_resolutions`: no time-based deletion; DSR-purge is the sole deletion path. Supersedes CL-21 (90-day-aggregate-and-drop) which was already marked superseded but had no successor on the books. Matches CL-330 pattern for owner_inputs + CL-390 cluster posture. VT-185 reframed v1.0 → v2.0 to "DSR-purge coverage for the 3 pipeline observability tables" (Critical, Sprint 1, was Phase 1.5 / Low). Privacy notice (per CL-389) must disclose lifetime retention + DSR deletion path. Triggered by Cowork filing VT-185 against the superseded 90-day spec; Clau flagged the conflict during VT-122 substrate briefing review; Fazal locked the policy.
- **CL-417** (2026-05-26, Clau-recommended + Fazal-locked, STANDING) — **α-SEQUENCING + CANONICAL SCHEMA GUARDRAIL.** (a) VT-187 schema normalization lands BEFORE VT-180 writer so VT-180 + all downstream consumers (VT-181/182/183/184/186 + VT-30 Composer + 7 confirmed VT-102/103/104/171/175/176/178 consumers) write canonical per-field columns from day one. β alternative (ship VT-180 with JSONB workaround, normalize later) = forced multi-site refactor. (b) Canonical schema is the design-doc §2.1 per-field shape; JSONB-envelope columns currently on main (`trigger_payload`, `terminal_state_metadata`) are interim only; after VT-187 lands, no new envelope-only read/write paths may be added. Missing per-field columns are NOT cosmetic (parent_step_id for SDK loop nesting; tokens_input/output for cost accountability; status for Ops UI replay queries). Triggered by VT-178 STEP-0 audit surfacing schema drift; Clau recommended α; Fazal locked.
- **CL-418** (2026-05-26, Fazal-issued, LOCKED/STANDING) — **DISCIPLINE RULE #17: CC must not stash untracked files during merge tasks.** Becomes Rule #17 in `docs/clau/discipline-rules.md`. Forbids `git stash --include-untracked` (`-u`) during any CC `task-merge` workflow. If working-tree obstacle, CC must signal Cowork + wait, not sweep. Single shared git index (Fazal + Cowork + CC + Claude chat) means CC's stash-sweep removes Cowork-side substrate from disk. Triggered by VT-30 merge bundling Cowork drift via `git commit -am` (precedent), then VT-178 merge stash-sweeping ~30 untracked files (recurrence). Companion rule from VT-30: CC uses explicit `git add <files>` (whitelist), not `git commit -am`. Recovery from VT-178 incident required Fazal terminal access to `rm .git/index.lock && git stash apply` (Cowork sandbox couldn't unlink at FUSE-mount layer).
- **CL-421** (2026-05-29, Fazal-issued, LOCKED/STANDING) — **ALL INTEGRATION-AGENT CONNECTORS MUST BE ZERO-MANUAL-PASTE AFTER OAUTH.** No Apps Script paste, no copy-paste secrets, no developer-shaped setup steps. OAuth grant + auto-configuration via vendor API is the only acceptable customer-facing flow. Triggered by VT-212 walk surfacing the Apps Script paste step as customer-hostile (target customer is a Tier-2/3 Indian SMB owner, not a developer). Sheet connector pivots to Drive Push Notifications (Files.watch) primary + 10-min polling fallback (filed as VT-222). Shopify (VT-213) already conforms — Custom App OAuth + auto webhook subscriptions. `setup_push` + `apps_script_template` deprecated; remove from happy path during VT-222. **[CORRECTION — CL-427, 2026-06-02: the "Shopify (VT-213) already conforms" claim is PREMATURE. The shipped path is client_credentials (dev/same-org ONLY); owner-facing zero-paste conformance lands only with the OAuth managed-install, VT-283.]**
- **CL-422** (2026-05-29, Fazal-issued, STANDING with launch-gate sunset) — **Dev Supabase project is accepted in ap-northeast-2 (Seoul). Production substrate provisions in ap-south-1 (Mumbai) before public launch.** Trigger: VT-169 region canary confirmed dev pooler + DB both in Seoul (`aws-1-ap-northeast-2.pooler.supabase.com`, IPv4 `3.39.47.126`, IPv6 `2406:da12:557:f801::/64`). Supabase free tier does not allow region selection; dev project was provisioned in default region. **Scope of acceptance:** dev environment use only — synthetic tenants + Fazal's RKeCom self-tenant + internal canary substrate. **Hard constraint:** NO real beta-partner customer data (phone numbers, ledger rows, WhatsApp message bodies) enters the dev Supabase project until prod-in-Mumbai is provisioned. Prod migration tracked as VT-231 (launch-blocker). VT-229 cancelled (decision rendered the manual-verification ask moot). DPDPA review at launch (VT-115) must confirm prod-only customer data with audit trail.
- **CL-423** (2026-05-30, Fazal-issued, STANDING) — **ALL PRs MUST REFERENCE A REAL NUMERIC VT ROW.** Every PR title must end with a parenthesised numeric `(VT-<N>)` (optionally `(VT-<N>-fix-<M>)`) where `<N>` is a real row allocated by `scripts/vt_id_allocate.py`. Text-suffix pseudo-parents (`VT-Foundation`, `VT-CI`, `VT-MCP-Tools`, `VT-FOO`) and dotted legacy subtask IDs (`VT-3.3a`) are NO LONGER accepted — the Notion `auto_increment_id` is retired and all new rows are numeric. Enforced mechanically by the `pr-title` CI gate (`.github/workflows/ci.yml`); the gate regex tightened from `\(VT-[0-9A-Za-z][0-9A-Za-z.]*(-fix-[0-9]+)?\)$` to `\(VT-[0-9]+(-fix-[0-9]+)?\)$`. Supersedes the 2026-05-18 pseudo-parents-lock convention. Triggered by Fazal directive 2026-05-30 ("All PRs must have a real numeric VT row") after session-close PRs #140/#141/#142 used `(VT-Foundation)`; filed + implemented as VT-239.
- **CL-424** (2026-05-30, Fazal-issued, STANDING) — **CC runs xhigh ultracode for all tasks; dynamic-workflow fan-out happens when the orchestrator warrants it.** GUARDRAILS (binding): (1) within a fan-out run, **VT-ID and migration-number allocation is assigned ONCE up-front, before the parallel phase** — never grabbed concurrently by parallel subagents (use `scripts/vt_id_allocate.py` + `scripts/migration_id_allocate.py`, both flock-serialized); (2) **one coherent PR per numeric VT row** regardless of subagent count; (3) Cowork **plan-first review still applies** on big/risky rows; (4) **one-PR-per-row + Pillar-7 Fazal-authorized merge unchanged**. Triggered by Fazal 2026-05-30 ("use ultracode workflows for all big and small tasks" + "Yes formalize it") with Cowork's serialize-allocation-under-fan-out guardrail. The migration-number allocator (`scripts/migration_id_allocate.py`, VT-249) closes the real concurrency gap: migration numbers were previously chosen by unlocked directory scan, so parallel subagents collided on the same number (e.g. VT-240 + VT-86 both reaching for 047). Filed + implemented as VT-249.
- **CL-425** (2026-06-01, Fazal-issued, STANDING) — **owner_inputs IS A SUFFICIENT LAWFUL BASIS UNDER DPDP for AI sub-processor transmission of customer PII** (owner-uploaded ledger images → Anthropic vision, VT-52). Fazal ruling (verbatim): *"owner_inputs is enough. Separate consent or a specific privacy notice is not required for this. The owner_input related lines in Privacy policy can be sufficient with the correct framing. You need not stop because of this."* NO separate sub-processor consent flag / dedicated notice required — the VT-52 vision pipeline's existing `_owner_inputs_enabled` fail-closed gate IS the control. Vision-path production enablement is NOT gated on new consent substrate — only on CL-422 (no real customer data on dev until VT-231 Mumbai). Complements CL-342 (vendor terms permit transmission) + CL-374 (Anthropic DPA). **Condition:** the privacy-policy owner_inputs language MUST state that enabling data inputs permits AI sub-processor processing incl customer-data transmission (framing follow-through = VT-272, Fazal/counsel-owned launch-gate, linked CL-389). Resolves VT-269.
- **CL-426** (2026-06-01, Fazal-issued, STANDING) — **VTR (Viabe Team Representative) = a DECAYING human-on-the-loop layer, NOT a permanent gate.** The agent operates autonomously; the VTR is the escalation target for business-KNOWLEDGE gaps and monitors daily/event activity. Locked points: (1) **Fazal = VTR #1** for the first cohort (founder-in-loop to observe real gaps). (2) **KG-injection is the accelerant** — pre-seed archetype knowledge to shorten the human period; VTR resolutions feed back into the KG (flywheel). Preferred = "human net powered by KG-injection"; no-human and agent-alone both REJECTED (unhedged bet on an unvalidated agent acting on a live business). (3) **Independence = a MEASURED THRESHOLD** (confidence + escalation-rate decay), NOT a date — rich archetype → days, novel business → weeks. (4) **Three-way routing (LOCKED):** agent acts when confident · VTR resolves business-knowledge gaps · OWNER holds authority (approvals/pricing/always-never, Pillar 7). Knowledge gap → VTR; authority/preference/customer-specific → owner. (5) **Privacy (Fazal ruling):** customer PII is **ENCRYPTED FROM THE VTR** — the VTR sees de-identified/business-level data only, never raw names/phones; this removes the human-PII-access surface (no separate human-access consent needed beyond CL-425). Any escalation needing customer identity → OWNER, not VTR. (6) **Owner interruption mode:** per-owner setting; batch voice-questionnaire = default, inline reserved for act-now needs. (7) **Instrument escalation-rate-per-category from day one** — flat decay = product bug; "1 VTR : hundreds" is contingent on decay, not a constant. Agent-OPERATION-layer rows rostered: VT-279 (escalation classifier knowledge-gap→VTR vs authority→owner), VT-280 (orchestrator→VTR daily+event digest), VT-281 (PII-encrypted-from-VTR de-identified view), VT-282 (escalation-rate + decay instrumentation); multi-VTR console DEFERRED → VT-189 (Ops Console V2, post-launch/data-informed). Triggered by Fazal 2026-06-01 (verbatim: *"I agree with your preference and assessment, you must document it accordingly... customer PII information encrypted so the VTR cannot see it. You must lock on to this."*).
- **CL-427** (2026-06-02, Fazal-issued, STANDING) — **CONNECTOR-AUDIT GATE + correction to CL-421's "Shopify already conforms."** (a) **CORRECTION to CL-421:** Shopify does NOT yet conform to the zero-paste owner flow. The shipped path (VT-208 / #221) is the OAuth2 **client_credentials** grant, which works ONLY for our own dev store (app + store in the SAME ORG) and is **dev/testing-only**. The owner-facing zero-paste path is the Shopify **OAuth managed-install** (authorization-code), filed as **VT-283** (PLAN-FIRST). Shopify conforms to CL-421 ONLY once VT-283 ships; the connector exposes BOTH modes (client_credentials own-store · OAuth-install merchants). (b) **STANDING RULE:** EVERY new connector (incl. future vertical ones) MUST pass the connector audit — *the owner ONLY approves; NO app-creation, NO scope screen, NO secret/token paste, NO developer-shaped step* — BEFORE it reaches owners. Reference: `docs/diagrams/viabe_connector_audit_16x10.png`. Triggered by Fazal 2026-06-02 connector-audit ("yes dispatch"). Ease follow-ups filed: **VT-284** (UPI forward-statement-to-WhatsApp, ease on VT-57), **VT-285** (POS OAuth-connect / file-upload fallback, ease on VT-58).
- **CL-428** (2026-06-04, Cowork-issued, STANDING) — **The `tenant_alerts.trigger_kind` DB CHECK constraint MUST stay synced to the `TriggerKind` Literal in `alerts/triggers.py`.** mig 089 (VT-76) reconciled a **VT-79 drift**: VT-79 added `tenant_isolation_breach` / `dsr_rate_anomaly` / `pii_in_log` to the Python `TriggerKind` Literal but no migration ever extended the DB CHECK, so `dispatch_alert._persist_alert` would have **violated the constraint on those detectors' first real fire** (masked until now only because they are gate-live / fire on empty data). **Discipline: any new trigger kind = BOTH the Python Literal AND a CHECK-extending migration, in the SAME PR.** mig 089 re-created the CHECK to the full 12-kind code set (8 mig-037 originals + VT-79's 3 + VT-76's `reconstitution_sla_breach`) rather than re-encode a known-wrong subset. Triggered by the VT-76 `reconstitution_sla_breach` addition surfacing the VT-79 gap (Cowork 20260604T041000Z accepted the bundled repair).
- **CL-429** (2026-06-06, Fazal-issued, STANDING — Pillar-7 amendment) — **MERGE-ON-GREEN self-merge for [BUILD]-class rows.** CC MAY self-merge a PR WITHOUT a per-PR Fazal auth when ALL hold: (a) the row was dispatched/acked as **[BUILD]**; (b) the **full suite is green**; (c) the diff touches **NO money/auth/PII/RLS/classifier code AND NO migration that alters data** (pure-additive index/doc/test/frontend-placeholder is OK); (d) the build **matches the dispatched scope** — any scope surprise → STOP + signal; (e) CC emits a **`merged` audit signal** per self-merge. Anything outside (a)–(e) → Cowork's gate, unchanged. Plan-first now applies ONLY to **money/auth/PII/RLS (+ VT-329)**; small classifier/frontend/tooling rows build directly with Cowork review at PR. Cadence: ≤2 unmerged self-merge-eligible PRs queued; merge serially. Everything else unchanged (serial-on-main, allocators, canaries, CL-418). Origin: Fazal verbatim *"Go ahead with the 4 change, lets close Sprint 8."* (2026-06-06), granted via Cowork 20260606T113000Z. [Clau: session-log entry CL-429 pending.] **AMENDED 2026-06-09 (VT-363/CL-432): CL-429 self-merge now targets the `dev` branch, NEVER `main`.** CC self-merges eligible [BUILD] rows into `dev`; reaching `main` is a separate Fazal-authorized promotion (CL-432).
- **CL-430** (2026-06-06, Fazal-issued, STANDING) — **ONE dashboard: the sprint dashboard. The PM dashboard is RETIRED.** Fazal ruling (verbatim): *"Lets get rid of the PM dashboard, and lets only have the sprint dashboard."* The sprint dashboard (`viabe-team-sprint-dashboard`, generated by `scripts/build_sprint_dashboard.py` from `.viabe/sprint/VT-*.md` + git, frozen to `.viabe/dashboard-approved-template.html`) carries the full filterable per-row board, so it subsumes the PM dashboard. Cowork tombstoned the `viabe-team-pm-dashboard` artifact; the two repo references (CLAUDE.md "How to find a thing" + the generator FOOTER/approved-template) were scrubbed to point at the one dashboard. The FOOTER/approved-template text change is a Fazal-authorized deliberate layout edit, NOT drift. Triggered by Fazal 2026-06-06; folded in via Cowork 20260606T143500Z (docs, CL-429 self-merge class).
- **Outbox at-rest retention — RULED (CL-437.3, Fazal 2026-06-12; supersedes the 2026-06-06 open line):** bodies retained only while needed for delivery/retry/replay/drain; on terminal completion the outbox body fields are REDACTED (metadata + hashes kept); exact owner-facing text lives only in the tenant-scoped owner conversation/audit surface under normal tenant retention + DSR. Implementation: VT-382.

- **CL-440** (2026-06-13, Fazal-issued, STANDING — product positioning) — **VIABE TEAM = the core product; VIABE REPORTS = an awareness feature, not a co-equal product.** Viabe Team is the core AI business platform that runs business tasks for owners autonomously — referred to in all contexts as **Viabe Team**. Viabe Reports is a feature offered to create awareness, attract founders, and promote the Viabe brand (a top-of-funnel play, not the core offering); externally and in all positioning it is referred to as **"Viabe's Location Feasibility Report"**, NOT as a standalone product. This is the brand/positioning frame every session must carry. *Architecture note:* Reports remains a technically separate codebase/DB/KG per the prior separation — "feature" here is the market/positioning framing, not a merge of the repos. Triggered by Fazal 2026-06-13 ("We need this positioning to be remembered, so that every session is aware of our offering").

- **CL-439** (2026-06-12, Cowork; entry `entries/CL-439.md`) — **The double batch CLOSED:** CL-437 privacy (VT-379 #462 `fc66400` + VT-382 #463 `44f8e25`, Fazal "accept" = reconstructible capture) + CL-438 (VT-383 #464 `87fcfd2` — ALL TEN templates Meta-APPROVED, drift ZERO, STOP #3 fell; VT-384 #465 `20a7106` — the L3/Act wire, owner STOP now freezes autonomy for real, STOP #1 fell; VT-385 design APPROVED, build awaits Fazal). **Counsel C1–C3 = the ONLY remaining gate to real customer sends.** Open Fazal items: owner_phone (live canary), counsel package, VT-231, the VT-385-build grant.

- **CL-438** (2026-06-12, Fazal-issued, STANDING; entry `entries/CL-438.md`) — **F1 LANDED (10 SIDs, EN+HI for the 5 agent-surface templates) + the AUTONOMY-FIRST principle.** Ask/Act permission model; repeated owner interruption = product bug; the L2→L3 ladder IS Ask/Act for sends; **F6 signed off; F3 delegated to Cowork defaults (20-streak, caps)**; non-bypassable Act-mode floor = opt-out/DSR + DPDP consent (C2) + money-bearing always-confirm + suspected monetary/reputational damage. C2 stays the structural stop on real customer sends until counsel. F4 caveat recorded (Hindi opt-out wording submitted ahead of counsel). Rostered: VT-383 (SID wiring), VT-384 (PR-3 L3 wire — now buildable), VT-385 (Ask/Act generalization design), VT-386 (registry health).

- **CL-437** (2026-06-12, Fazal-issued, STANDING; entry `entries/CL-437.md`) — **Three privacy rulings:** (1) VT-379 dispatch granted (error-column redaction). (2) **VTR-PII lock REAFFIRMED + extended to UPDATES** — "the VTR cannot update or see the customer PII fields"; the UI-masking/partial-visibility model REJECTED after the CL-425/426 conflict was flagged; do not re-propose. (3) The outbox retention policy above.
- **Counsel-package dedupe** (2026-06-06, Cowork triage 20260606T204500Z) — **VT-156 + VT-272 + the VT-353 binding copy all hang on ONE counsel package; stop triple-tracking.** The package = privacy notice + the owner_inputs/AI-sub-processor framing (CL-425 condition) + the public legal copy (privacy/dpdp/terms). It is a SINGLE Fazal/counsel action; **VT-156 is the gating row.** VT-353 ships wired bilingual DRAFT shells now (banner-marked, unlinked) — the BINDING copy stays NEEDS-FAZAL under VT-156. Don't roster separate copy-tracking rows.
- **CL-425/CL-426 PII-from-VTR — DB-ENFORCED ON ALL VTR PATHS** (2026-06-07, flipped on VT-360 merge; staged-go-live history below). As of VT-360 the guarantee is literally true end-to-end: **"CL-425 (PII-unreachable-from-VTR): DB-ENFORCED on ALL VTR paths. The orchestrator/digest path (VT-280) and the team-web ops surface (VT-360) both read customer/operational data ONLY through the VT-281/360 de-identified views as app_vtr_role (NO grant on raw tables / decrypt) — PII is unreachable, not merely masked. maskForVtr + the raw-id REF# are deleted; the VTR surface holds no service-role raw read. The sole PII door is the audited [Resolve] path (VT-188). PRECONDITION for any second VTR: the views are not yet assignment-scoped (Phase-1 = Fazal-as-VTR#1 sees all tenants) — a 2nd VTR requires the VT-281 assignment-scoping (WHERE tenant_id IN vtr_assignments) FIRST."** _History (staged, Cowork 20260607T131500Z): "DB-ENFORCED on the orchestrator/digest path as of VT-280; team-web APP-SIDE-MASKED (maskForVtr) until VT-360 lands; interim exposure structural only — sole VTR = Fazal." — superseded by the VT-360 flip above._
- **Live canary mandatory for vendor response-shape contracts** (2026-06-09, Cowork close-signal 20260609T094500Z; surfaced by VT-361 / PR #424). **A mock-based shape test can encode the vendor's own bug and pass green forever — the VT-361 Sandbox client's `test_sandbox_kyc_shape` pinned a single-level response shape that did not exist; the real API nests the GST record at `data.data`, so `search_gstin` returned `ok=True` with all-None fields while every unit test was green.** Standing: for any external vendor whose RESPONSE shape we parse, a live canary (real call, real creds, fail-not-skip) is the mandatory gate — not optional, not substitutable by a mocked structural test. The mock pins the request contract; only a live call proves the response contract. Reinforces Rule #15 (the canary must actually RUN, and CC runs it — CL-429 / CLAUDE.md 2026-06-09).
- **CL-431 — CC infra-env authority + secrets hygiene** (Fazal, 2026-06-09; AskUserQuestion ruling, via Cowork 20260609T130000Z). Fazal granted CC direct console access to **Railway**, **Vercel**, and **Supabase** (both projects) and ruled the env-var authority model: **CC manages DEV env vars autonomously** (Railway Dev env, `viabe-team-dev` Vercel, Seoul Supabase). **Every PROD env-var change — config OR secrets — requires explicit Fazal authorization first** (the Pillar-7 analog for infrastructure). New scope = new grant; a dev grant never implies prod. **Secrets hygiene (binding):** CC NEVER writes a live secret VALUE into any repo signal, log, PR, or commit (the repo is git — a secret echoed into a signal file is a committed secret). CC sets/rotates secrets directly in the console and reports ONLY the variable NAME + action ("set/rotated `<VAR_NAME>` in `<env>`"), never the value. Composes with the no-local-live-secrets rule (Fazal 2026-06-07): live creds go console→Railway/Vercel prod directly, never staged locally, and now never echoed through CC's signals.
- **CL-432 — dev-branch staging + the main-promotion gate** (Fazal, 2026-06-09; VT-363). TWO long-lived branches: **`dev`** → Railway Dev + Vercel `viabe-team-dev` + Supabase Seoul (the deployed, phase-E2E-tested env); **`main`** → Railway Prod + Vercel `viabe-team` + Supabase Mumbai (deliberate, real customer data). **CC's default PR base is `dev`; CC self-merges [BUILD] rows on green into `dev`** (CL-429 retargeted), risk rows still Cowork-gated first. **`main` is Fazal-authorized ONLY** — CC NEVER merges to `main` without an explicit Fazal `type: task` promotion instruction (the new Pillar-7 gate, the infra analog of per-merge authorization). A `dev`→`main` promotion PR opens only on Fazal's word (Cowork relays); a PR targeting `main` is forbidden unless it IS that authorized promotion. Flow: feature → PR into `dev` → CC self-merge on green → Dev deploy → phase E2E → Fazal authorizes dev→main promotion → Prod. Deploy: a `main` merge MUST NOT auto-ship to the Mumbai prod env — prod deploy is manual (`workflow_dispatch`/environment-gated), not auto-on-main, for the first launches. Codified in CLAUDE.md "Merge workflow → Branch governance". Topology (Railway 1-project/2-env, Vercel `viabe-team-dev`+`viabe-team`, Supabase Seoul-dev/Mumbai-prod) is codified in CLAUDE.md "Deploy topology → Two-environment model". **Refinements (Fazal, 2026-06-09 via Cowork 20260609T135500Z — supersedes the 20260609T134500Z draft, whose "not even by-reference" wording was wrong: it would forbid running prod migrations at all):** _(A) Supabase PROD credentials — Fazal sets the VALUE; CC never READS it, but a process CC LAUNCHES may CONSUME it._ The Mumbai (prod) connection string + **service-role key** (RLS-bypass on the real-PII DB = the highest blast radius) are set by **Fazal directly** into Railway/Vercel PROD env + the Supabase console. Precise rule: **CC never READS the plaintext** — no `cat`/`echo`/`print`, never opens a file containing it, never into a signal/log/PR (reading plaintext = it enters CC's context = transmitted to Anthropic that turn). **But a process CC LAUNCHES may CONSUME it from an injected env** — e.g. `railway run --environment <prod> python apps/team-orchestrator/scripts/apply_migrations.py` injects `DATABASE_URL` from Railway prod into the subprocess, which reads `os.environ`; the value flows OS-env→process, NEVER into CC's token stream. CC runs prod migrations without ever knowing the credential, and reports ONLY the result (e.g. schema_migrations count) + the var NAME. **CC never TYPES the literal prod secret value into any command** (injection only). _(B) Secret-by-reference for the prod secrets CC DOES set_ — `railway variables set KEY="$(<source-cmd>)"` in a subshell, value never printed; **FORBIDDEN: any command that echoes a secret to stdout** (same rationale — CC is a Claude model, plaintext in context → Anthropic). _(C) Prod migration runs are Fazal-authorized_ — running migrations against the Mumbai prod DB is a prod-impacting operation (Pillar-7 spirit): Fazal authorizes the run, CC executes via the injection method above; NOT a free action inside a dev grant. _(D) File naming_ — `.viabe/secrets/supabase-dev.env` stays **dev (Seoul) creds only, name unchanged** (NOT supabase.env); prod Mumbai creds never go in a local file (per A). _(E) Environment isolation (Fazal HARD requirement, 20260609T140500Z — dev/prod must NEVER jumble):_ (1) **explicit env always** — every `railway run` / `railway variables` command MUST pass `--environment <dev|prod>` explicitly; a bare command using the linked/default env is FORBIDDEN (that's how a jumble happens). (2) **one env per shell/invocation** — never source `supabase-dev.env` (dev `DATABASE_URL`) in the same shell that runs a prod-injected command, since an ambient dev `DATABASE_URL` could shadow the injected prod one (the runner takes `os.environ["DATABASE_URL"]` first); prod runs happen in a clean invocation whose ONLY `DATABASE_URL` source is the railway-prod injection; dev work uses supabase-dev.env, prod work uses railway-prod injection, never mixed in one command/loop. (3) **structural pre-flight guard (VT-362):** `apply_migrations.py` REFUSES to apply unless the connected DB matches an explicitly-passed `--expected-env {dev|prod}` — a sentinel table `app_environment` (one row) is read + asserted before any migration, with a bootstrap non-secret host-ref check on a fresh DB; no silent apply to whatever `DATABASE_URL` happens to be set. (4) **no seed/synthetic data ever runs against `--expected-env prod`** (CL-422), enforced by the guard.

- **CL-434 — 6-gap build CLOSED on dev; standing surfaces locked** (Cowork, 2026-06-11; entry `entries/CL-434.md`). Single send choke point `agent_send_draft`; THREE independent customer-send stops (no L3 caller / empty `MARKETING_CONSENT_VERSIONS` / null Meta SIDs — F1+C2 flip together, each = CL entry + re-canary); opt-out/DSR matcher BEFORE any reply-consuming gate; owner-confirmed draft pattern; frozen-bundle citation validator (text_hi + sentence-leading gaps = pre-promotion); atomic kill switches fail-CLOSED; new PII tables get FORCE RLS + `_PURGE_ORDER` day one.

- **CL-435 — Clau RETIRED from the loop; Cowork owns architecture; VTR Run-Control Panel A–D granted** (Fazal, 2026-06-11; entry `entries/CL-435.md`). Clau exits the four-role model — **Cowork absorbs implementation strategy, cross-sprint sequencing, and audit** (adversarial-subagent gates on plans + built code with executed evidence). Same directive: **Run-Control Panel phases A–D, session-blanket autonomous** — VT-374 (substrate; MERGED #458 b8bd111), VT-375 (read-only canvas; MERGED #459 9213024), VT-376 (interactive controls), VT-377 (multi-VTR capability-complete — Fazal ruling: no human onboarding). Interactive-CC mode (Fazal ruling). Invariants: VTR I/O reads redacted surfaces (CL-425/426); re-runs/forks NEVER inherit approvals nor bypass send/opt-out/consent gates (gate steps structurally non-overridable, manifest + import-raise + CI grep); control rows RLS+FORCE+purged+audited.

- **CL-436 — VTR Run-Control Panel A–D batch CLOSED on dev** (Cowork, 2026-06-12; entry `entries/CL-436.md`). VT-374 #458 `b8bd111` (substrate) / VT-375 #459 `9213024` (canvas) / VT-376+380 #460 `0192d60` (controls) / VT-377+381 #461 `98560e6` (multi-VTR capability). Locks: gate modules structurally non-controllable; pause never delays opt-out/DSR; rerun = re-dispatch, never inherits approvals; VTR visibility keys-only; assignment scoping fail-closed; admin tier = role-gated. Devanagari validator pre-promotion item CLOSED WITH BOUNDARY; sentence-leading gap stays OPEN; 2nd-VTR preconditions extended (vtr_admin_connection audit-or-split). VT-379 Queued NEEDS-FAZAL.

- **CL-433 — 30-day free trial, no card in trial, opt-in subscribe, NO refund ever** (Fazal, 2026-06-09; Gap-1 / VT-365, via Cowork plan-approval 20260610T003000Z). **SUPERSEDES** the Phase-1 concept's "recover 2× the fee within 39 days or auto-refund" guarantee, and **retires the entire refund subsystem** (VT-85 day-39 refund-offer engine, VT-92 day-39 evaluator, VT-93 `execute_refund`, the refund reply-classifier, the refund WhatsApp templates `refund_offer`/`refund_completed`, the refunded-tenant dispatch block) **and trial extensions** (VT-90 `trial_extension_*`). **New model:** a flat **30-day** free trial with **NO card captured during the trial**; the owner **actively subscribes** at/after day 30 (an explicit action — there is **NO auto-charge edge**); a trial that elapses moves to the dormant, **re-subscribable `lapsed`** phase; **NO refund for any reason.** State machine: `onboarding→trial→{subscribe→paid_active | trial_expired→lapsed}`, `lapsed→subscribe→paid_active`; the `gstin_verified` activation gate moved off `card_captured` onto `subscribe`. Razorpay `payment.captured` now maps to the `subscribe` event (a capture can only follow an explicit subscribe — no card in trial → nothing to auto-charge). Implemented in VT-365: transitions/state reshape, refund-subsystem deletion (grep-zero in prod src), migration `121` (drops `refund_executions` + `day39_evaluations`, reshapes the tenants/subscriber_states phase CHECK to add `lapsed` + drop `trial_extended`/`refund_offered`/`refunded`, drops `refunded_at` + `trial_extension_count`), Twilio template registry retired. The Devanagari/Hinglish **opt-out/DSR negation** work SURVIVES in `pre_filter_gate` (the authoritative matcher) — only the refund-specific reply classifier was deleted. Class: money-path → **NOT self-merge**; Cowork adversarial-subagent gated before the dev merge (Fazal away, dev-build delegated to Cowork — CL-432 `main` gate unchanged).

- **CL-442** (2026-06-24, Fazal-issued, STANDING — re-rules the VT-361 gate LOCATION) — **THE GSTIN HARD-GATE MOVES FROM ACTIVATION TO SIGNUP.** Fazal (verbatim 2026-06-24): *"We will be gating it hard, a no-GST business doesn't get anything, neither paid nor trial."* **Invariant:** no tenant reaches trial OR active without `verification_status ∈ {gstin_verified, vtr_verified}`. The VT-361 gate at `subscribe → paid_active` (transitions.py) is NOT removed — it is RETAINED as defense-in-depth; the PRIMARY gate is now at signup/account-creation. Enforcement = **verify-then-create**: GSTIN verified server-side BEFORE the `tenants` row exists, so no unverified tenant is ever persisted (DPDP: nothing held for a rejected business; CL-390/422 posture). `vendor_down` ≠ reject — it HOLDS with a retry path (an outage must not turn away a legit GST business) and is CAP-EXEMPT from the owner's 5/day lookup budget (the per-IP throttle is the abuse backstop); `invalid_gstin` REJECTS to a graceful "Viabe Team is for GST-registered businesses" screen, generic copy with NO enumeration oracle (inactive-vs-not-found never leaked) — the VT-406 "use my typed name / no-match" path NO LONGER proceeds unverified. The `tenant_provision` inbound backdoor is CLOSED for NEW/UNKNOWN numbers (an unknown inbound gets a "verify at signup" reply, no tenant); the known-tenant merge path is untouched. Supersedes the kyc-decision.md "Activation gate" framing (gate is signup, not card_captured). Build = VT-408; the verify round-trip + `gstin_verified` set + discovery anchor is VT-406. Triggered by Fazal's hard-gate ruling during the Sundaram e2e.
- **CL-443** (2026-06-25, Fazal-issued, STANDING — UX architecture) — **WHATSAPP-FIRST: the conversational agent is the PRIMARY tenant surface.** The tenant journey runs primarily in chat on WhatsApp, driven by the conversational agent. Any step needing complex input/output — integration/OAuth connect, file/CSV upload, forms, subscribe/pay, settings, reports — opens as a **link into the WhatsApp in-app browser**, the owner completes it, and control **returns to the conversation** (the VT-267 handoff+resume pattern). The form/wizard pieces are **reused as those embedded link-outs, NOT a parallel wizard product.** Standing principle **beyond onboarding** (subscribe/pay, settings, reports all link-out from chat). Re-scopes **VT-425** (from "launch on the form wizard + quarantine the agent stubs" → "conversational-agent onboarding as primary; wizard steps reused as WA-browser link-outs"): reuse the built setup endpoints (Sheets/WhatsApp VT-415, Shopify OAuth VT-422) as integration link-outs; de-stub the 5 `integration_agent` tools for real (VT-417 primitives exist); net-new = a file/CSV upload form + a field-mapping confirm form (both WA-browser link-outs) + the `tenant_field_mapping` store (migration 142). PII: real data only post-VT-231; dev = Fazal-controlled INTERNAL only (never-add-numbers binds; CL-422/425/390/104). Fazal verbatim 2026-06-25: *"We are more inclined towards keeping the tenant interaction mostly on WhatsApp… we will need the conversational agent built, as that will be the primary path of interaction with the tenant."* Plan-first → Cowork gate.

---

## Notes on this reconciliation pass

**6 supersessions marked:** CL-20, CL-21 (privacy cluster → CL-390); CL-32 (→ CL-175); CL-33 (→ CL-35/CL-36); CL-57 (→ CL-324); CL-177 (→ CampaignPlan v1.0 via CL-260). Plus secondary supersession chains marked for the snapshot sequence: CL-307→CL-309→CL-317→CL-325→CL-375→CL-391→CL-394→CL-407.

**5 additions (2026-05-25 pass):** CL-260 (CampaignPlan v1.0 by-effect), CL-330 (owner_inputs structured-intent — THE current critical-path decision), CL-389 (privacy notice system-level framing), CL-407 (latest anchor). Discipline rules #12 (CL-322) and #13 (CL-324) re-tagged with the rule numbers in their lines so they're explicit as rule entries.

**3 additions (2026-05-26 pass — VT-178 / VT-122 substrate cycle):** CL-416 (Fazal STANDING: pipeline-observability retention lifetime-of-relationship; supersedes CL-21 gap; reframes VT-185); CL-417 (Clau-recommended + Fazal-locked STANDING: α-sequencing — VT-187 before VT-180 — + canonical schema guardrail); CL-418 (Fazal STANDING: Rule #17 — CC must not stash untracked files during merge tasks). Three load-bearing standing decisions surfaced during the VT-178 merge cycle + Clau briefing review.

**1 dedupe:** CL-385 and CL-386 collapsed onto CL-386 line (kept the formal Fazal-approved entry).

**1 unverifiable reference:** Clau flagged UUID `366387c2-cc5a-81f1` as the CampaignPlan v1.0 / VT-37 page. Grep against `docs/clau/entries/*.md` AND `.viabe/sprint/*.md` returns zero matches. Likely either a UUID transcription glitch in Clau's note, OR a Notion sprint-board page that's no longer in the live data source. CL-260 is the strongest evidence available for the v1.0 decision so it's the citation used.

**4 discipline rules still TODO** at `docs/clau/discipline-rules.md`: #6, #7, #10, #11 — no CL entries define them. Possibly renumbered duplicates of early rules. Awaiting Clau dump or confirmation.

**Per Rule #14 itself (CL-386):** every line in this ledger has been verified against its source `entries/CL-<N>.md` file. The reconciliation is not from memory.

---

**CL-2026-06-28-push-authority (Standing, Fazal 2026-06-28).** CC granted authority to push `origin/dev` WHEN REQUIRED — no longer Fazal-explicit-per-push (supersedes the 2026-06-27 explicit-push rule). Guardrails: push at a deployable checkpoint (coherent, green, gate-passed unit), BATCH commits into one push, pre-push hook green, `main` stays Fazal-only. Paired with the finished-product mandate (CC drives the whole e2e — happy + unhappy paths — for Fazal's single sign-off, not a UAT). Source: 20260628T201500Z Cowork mandate carrying Fazal's 20:15 directive.

**CL-2026-06-28-dev-consent-activation (dev grant, Fazal 2026-06-28).** On DEV ONLY, `MARKETING_CONSENT_VERSIONS=winback_optin_v1_dev_2026-06` is set + +917738859946 (Fazal's internal number) gets a matching `record_of_consent` so SR-detect surfaces it for the e2e. The C2 prod-boot guard is UNCHANGED — PROD `MARKETING_CONSENT_VERSIONS` stays empty/counsel-gated, never set. CL-422 holds (no real customer data on dev). Source: 20260628T202500Z.

**CL-2026-06-28-cc-full-autonomy (Standing, Fazal 2026-06-28 21:30).** CC has FULL control — no Cowork gate-before on anything (risk rows included). CC owns the gate (self adversarial-verify + no-drift self-check before landing) + self-merges/pushes dev at checkpoints. Cowork = AUDIT-AFTER (verifies what lands vs the agreed journey + raises to Fazal); never blocks CC. Blockers/needs → Fazal DIRECTLY in the terminal, not a to-cowork-and-wait. Still binding (not Cowork gates): main Fazal-only; no-drift contract (203500Z); keep correctness gates real; no number/data unless Fazal-provided; dev harnesses prod-safe; no real send pre-signoff. Source: 20260628T213000Z.

**CL-2026-06-28-team-manager-rebuild (Standing, supersedes CL-24; Fazal-ratified FOLD-IN 2026-06-28).** The owner-facing brain = a reasoning **Team-Manager (Business-Executioner/Team-Leader)** that understands owner intent and delegates to domain specialists — superseding CL-24's "brain = router, NOT a domain reasoner." Still NOT the domain executor (specialists are); still NOT the writer/sender (tools own writes); **the safety/correctness gates stay DETERMINISTIC, non-bypassable RAILS** the brain must route through (it has no code path to any side-effect except via a guarded tool). "Nothing hardcoded" = dynamic BEHAVIOR, fixed RAILS. Sign-off FOLDED IN: the win-back send + single sign-off wait until the new-brain live e2e re-drive is clean. Canonical design: `docs/clau/team-manager-rebuild-design.md`. Build order VT-460 (rail harness, FIRST) → VT-461 (supervisor) → VT-462 (onboarding-conductor) → VT-463 (handoffs) → VT-464 (e2e re-drive). Source: Cowork 20260628T204000Z + Fazal FOLD-IN 204500Z.

**CL-2026-06-28-full-six-manager (Standing, Fazal 2026-06-28; extends the team-manager-rebuild Standing).** The Team-Manager manages the WHOLE business — Sales, Marketing, Finance, Accounting, Tech, Cost-Optimisation — and sign-off WAITS until all six lanes are built + wired. Division of intelligence: **manager = situation + outcome + which-specialist + cross-functional tradeoffs (never the action, never needs domain expertise); specialist = decides the action in its lane (two-way handoff — can push back + propose a better outcome)**. Rails EXTEND to business-impact (spend/send/commit/config → owner-gated guarded tools, threshold-based, decaying-HITL reusing the VTR model) on top of the compliance rails. Foundation builds now; lanes build to Cowork-drafted/Fazal-ratified charters, proven incrementally. VT-465 roster-registry, VT-466 KG/context store, VT-467 business-impact rails, VT-468..472 the five new lanes (Sales=VT-463/SR). Source: Cowork 205500Z/210500Z + correction 211500Z.

**CL-2026-06-29-charters-and-send-checkpoint (Standing, Fazal/Cowork 2026-06-29).** The 6 lane charters are ratified (Sales VT-468, Marketing 469, Finance 470=advisory-never-moves-money, Accounting 471=prepare-only-v1, Tech 472, Cost-Opt 473=advise-v1) — v1 = advise/act-within-policy; future autonomy (Accounting file/submit; Cost-Opt act-on-recalibration) is architected-for but NOT built (gated behind explicit Fazal grant + regulatory auth). Autonomy hardening: "within policy" = a DETERMINISTIC machine-enforceable bound-check (not the brain's judgment); escalation = concrete deterministic triggers. SEND = a DECAYING CHECKPOINT (owner-visible first sends → decays to autonomy once proven; reuse VTR decay), not per-send-forever. VT-474 builds the policy-bound/escalation/send-checkpoint rails; the lanes depend on it. Source: Cowork 20260629T073500Z carrying Fazal's ruling.

**CL-2026-06-29-validate-on-dev (Standing, Fazal 2026-06-29).** ALL validation happens on DEPLOYED DEV — never locally. Dev is the validation branch. A dead/absent local Anthropic key is NOT a blocker: always push to dev (CC push authority) and validate the re-drive there (Railway holds the valid LLM key). Stop blocking the push/re-drive on local-LLM availability. Source: Cowork 20260629T101000Z carrying Fazal's directive.

## CL-2026-07-01-single-owner-only — Launch = single-owner-only (Fazal 2026-07-01, Standing)
Launch persona = single-owner SMB: the owner IS the business. Dedup on the owner's `whatsapp_number` (existing signup.py ON CONFLICT). **NO GSTIN/business-uniqueness constraint** — a GSTIN is per-legal-entity-per-state; a chain shares ONE GSTIN across MANY stores, so a GSTIN-block would wrongly reject legitimate second stores. Multi-store/enterprise (one GSTIN → many store-manager tenants; store-level/delegated/human verification; parent-org seam) is **fully PARKED** (no build, no seam, no doc) until enterprise scope. Originated VT-513 (CANCELLED the GSTIN-uniqueness build). Do not re-add a GSTIN-block.

## CL-2026-07-01-dev-full-parity — Dev has full parity with Live (Fazal 2026-07-01, Standing)
Anything needed for launch is built + available on DEV now — no deferring to a prod cutover. Reinforces validate-on-dev (CL-2026-06-29). Originated the VT-514/515/516 observability BUILD-NOW (build on dev immediately, not plan-then-prod-later).

## CL-2026-07-01-phase1-locked — Phase-1 plan LOCKED as the governing contract (Fazal 2026-07-01, Standing)
The Phase-1 plan (`docs/clau/phase1-plan.md`, formerly `-PROPOSED`) is LOCKED/Standing — the single governing contract for the build to Concierge-Mode launch. Execution order: `B1 → B2 → B3 → B4 → B5 → B6 (+ C2a self-handling + C3 capture substrate) → technical soak → C2b → C3 retrieval → C4 graduation`. C1 (effect-boundary determinism) is a design principle honoured throughout Track B; Tracks A + D run in parallel throughout; the reviewer + owner-policy contracts are launch foundations delivered alongside B4–B6. Scope-lock: after lock, no new Phase-1 features — only closure, verification, and launch-defect fixes. Delivered under the overnight autonomous grant (Cowork 20260701T200000Z carrying Fazal's directive).

## CL-2026-07-01-manager-tasks-canonical — manager_tasks/manager_task_steps = canonical task state (Fazal 2026-07-01, Standing)
New canonical tables `manager_tasks` + `manager_task_steps` hold manager-task state. Existing orchestration is RECONCILED under them, not replaced: `business_plan` = long-term roadmap; `agent_work_items` = autonomous roadmap execution; `pipeline_runs` / durable workflows / approvals / effects = LINKED execution evidence. Canonical hierarchy: `manager_task → task_step → pipeline_run / workflow / effect`. No other table may independently claim manager-task status. (Plan B2 — the one locked storage decision.)

## CL-2026-07-01-task-store-retention — manager-task + correction-store retention = lifetime-of-relationship, DSR-only deletion (Fazal 2026-07-01, Standing)
`manager_tasks`, `manager_task_steps`, and the C3 outcome/correction store carry retention = lifetime-of-relationship; the SOLE deletion path is a data-subject request (DSR-purge). Each ships its privacy lifecycle IN ITS OWN MIGRATION: tenant RLS + `FORCE ROW LEVEL SECURITY`, DSR-purge registration, no raw phone/body/name columns, reviewer access only via de-identified assignment-scoped views, and defined referential-deletion behaviour for linked evidence. Extends CL-416 (pipeline-observability retention = lifetime-of-relationship).

## CL-2026-07-01-concierge-day-zero — Day-zero launch = universal consequential-action VTR review (Fazal 2026-07-01, Standing)
Concierge Mode is the launch posture: at launch a trained Viabe Team Rep (VTR) reviews EVERY consequential action. This is day-zero of a DECAYING human-in-loop model (per CL-426) — autonomy is EARNED per capability from measured clean outcomes, never unlocked by elapsed time. Fazal is VTR #1; a second reviewer cannot be added until per-tenant assignment scoping is enforced (a reviewer sees only assigned tenants; customer data encrypted from the reviewer). The numeric VTR review SLA + per-reviewer capacity limits are **Fazal-approved before launch** (they bound owner-response time + gate admission scaling).

## CL-2026-07-01-launch-date-target-if-green — 1-August = target-if-green; non-waivable gates override the date (Fazal 2026-07-01, Standing)
Quality gates take priority through 31 July; Concierge Mode launches **1 August only if green**. The non-waivable gates — privacy, tenant isolation, ownership verification, consent, send safety, data-subject rights, production-environment readiness — are NEVER waived and OVERRIDE the date: any red on 1 August delays launch until it is green. Several gates have third-party lead times, so 1 August is a target-if-green, not a commitment.

## CL-2026-07-01-sr-playbook-bar — SUPERSEDED by CL-2026-07-01-no-fixed-playbook
~~The Track-D initial acceptance bar for the Sales Recovery playbook is ≈100 reviewed notes…~~ Reversed same day — see below.

## CL-2026-07-01-no-fixed-playbook — NO fixed authored playbook; advice = LLM + learnable-memory under a no-fabricated-numbers rail, measured by a held-out eval (Fazal 2026-07-01, Standing)
Kill the ~100-note playbook AND the 69-note SR retrofit. Fixed notes = a cage; the agent never self-learns beyond them. **Knowledge = the LLM's own reasoning + the C3 memory-learning loop** (learn from real success/failure) — this is the moat, not a static corpus. Kept from old Track D, reframed: the ONE surviving guardrail is **no fabricated numbers/benchmarks** (a factual claim must be grounded or explicitly hedged) — a claim-grounding output rail, not a knowledge base. A **held-out advice-quality EVAL** (factuality/actionability/relevance/tone) stays as MEASUREMENT before a capability graduates; it measures LLM+memory output, never authors/scripts/confines advice, and is not a retrieval corpus. **SEED the learnable memory** (CL-426 KG-injection accelerant) to shorten cold-start — the seed is **mutable SEED MEMORY** the agent grows beyond, NOT a fixed note-set; default = seed (not empty). Build the seedable-memory MECHANISM now (part of C3); seed CONTENT is a separate Fazal/archetype follow-up. Cold-start = concierge (VTR reviews day-one advice; corrections feed memory). C4 graduation thresholds remain Fazal-approved before first graduation.

## CL-2026-07-01-dev-send-allowlist — DEV_SEND_ALLOWLIST = 4 Fazal numbers (Fazal 2026-07-01, Standing)
`DEV_SEND_ALLOWLIST` (dev orchestrator env; the `dev_send_guard.py` mechanism already exists — mocks any send to a non-allowlisted `to` when EXPECTED_ENV!=prod) = the four Fazal-PROVIDED numbers only: `+919321553267`, `+919820463598`, `+917738859946`, `+919892616965`. Enables Fazal's own-phone dev testing + the B1/VT-524 real delivery-callback canary. Non-allowlisted destinations stay mocked; NO real customer send; the never-fabricate-numbers rule still binds (these entered via an explicit Fazal grant). A welcome-delivery canary additionally waits on Meta approving `team_welcome3`.

## CL-2026-07-01-observe-only-rails — new decision/policy rails land OBSERVE-ONLY first, flip to enforcing under an explicit gate (CC build-discipline, 2026-07-01, Standing)
A rail that could change live routing/sending (the B3 manager decision loop, the OC1 customer-send policy bound) lands in TWO steps: (1) an **observe-only** slice — the rail EVALUATES on the real live path and RECORDS the would-be decision/block to `tm_audit` (`event_layer='decides'`, `status='observed'`; kinds `manager_decision` / `policy_shadow`), but does NOT alter the return/routing; fully fail-soft; (2) the **enforcing flip** — a later, separately-authorized slice, taken only once the observe-only data shows it is safe (e.g. OC1's `enforce_policy=True` waits on seeded policy grants; B3's routing-steer waits on measured decision quality). Rationale: the load-bearing paths (LangGraph dispatch, the customer-send choke) must not be rewired blind — shadow first, measure, then flip. The observe-only slice is NOT a stub: it runs on real traffic and produces the operational substrate the flip decision needs. Precedents: VT-548 (B3-wiring), VT-533 (OC1), the pre-existing `enforce_policy` opt-in (VT-474).

## CL-2026-07-02-vtr-capacity — VTR capacity = 3 VTRs × ≥100 tenants (~300); 1:100 is the GRADUATED target (Fazal 2026-07-02, Standing)
The VTR-contract capacity = **3 VTRs, each guiding ≥100 tenants** (target ~300). 1:100 is the GRADUATED-STATE target — contingent on capability graduation + escalation-rate decay (CL-426), NOT a day-one concierge constant; admission ramps into the 300 as capabilities graduate. Fazal = VTR#1 (all tenants via the `app_vtr_admin_role` role leg) until per-tenant scoping is confirmed live for the 2nd/3rd.

## CL-2026-07-02-vtr-scoping-already-built — the multi-VTR precondition is ALREADY BUILT (VT-377); no rebuild (CC reconciliation 2026-07-02)
Cowork's 20260702T0900Z rostered "per-tenant VTR assignment-scoping" as a NEW prerequisite build. **Rule #14 reconciliation: it already exists, end-to-end.** DB: `migrations/134_vt377_vtr_assignment_scoping.sql` assignment-scopes all NINE `app_vtr_role` de-identified views via `operator_assignments` (mig 072) + `app_vtr_operator()`; admin (Fazal=VTR#1) sees all via the role leg; `require_vtr_action` gates per-tenant. UI: `apps/team-web/app/(app)/team/ops/assignment/actions.ts` (VT-295) assigns/unassigns. Tests: `test_vtr_assignment_scoping_realdb.py` + `test_operator_assignments_substrate.py` (18/18 green 2026-07-02). VT-377 explicitly RETIRED the `vtr_assignments`-table idea as divergence-by-construction — the substrate IS `operator_assignments`. So the 2nd/3rd VTR is NOT DB/UI-blocked; the residual is operational (assign tenants to the new VTRs + confirm their JWT/role wiring), not a build.

## CL-2026-07-02-drop-clau — Clau REMOVED from the role model; three roles now (Fazal 2026-07-02, Standing)
Clau (Architecture Advisor / audit-AFTER layer) is REMOVED. The model is now THREE roles: **Fazal** (decides), **Cowork** (routes + reconciles + audits-after — the audit layer that was Clau's now sits with Cowork per the 2026-06-28 CC-full-autonomy decision), **Claude Code** (builds + logs + self-gates). No audit-AFTER routing to Clau; no sprint-boundary Clau review; the resurrection-file owed by Clau is moot. `operating-brief.md` §7a Clau-review loop is superseded by Cowork audit-after.

## CL-2026-07-02-implicit-feedback-weak-signal — implicit outcome feedback = WEAK/contextual signal only, never correction-grade (Fazal 2026-07-02 "Option C", Standing)
The VT-563-activated implicit-attribution sweep (VT-432 rule: below-baseline outcome ⇒ implicit thumbs_down) KEEPS capturing — but an implicit outcome row is a **down-weighted, distinctly-tagged prior** (`owner_feedback.tier='implicit'`), NOT correction-grade evidence. It must NEVER (a) carry the weight of an explicit human (owner/VTR) correction, or (b) by itself graduate or demote a capability. The C4 accuracy bar counts **explicit human feedback + hard evidence** (delivered / opt-out / read receipts) as authoritative; implicit outcomes enter decision context only as weak context under the C3 controls (tenant-scope, provenance, authority). Any future consumer of `owner_feedback` MUST branch on `tier` and down-weight `implicit`. Origin: vision-audit finding surfaced by CC 2026-07-02; Fazal picked Option C (relayed via Cowork 20260702T163000Z).

## CL-2026-07-03-agentic-fleet-mandate — EVERY agent gets a brain-commanded tool belt + self-evolving, compacting memory (Fazal 2026-07-03, Standing)
Not only the onboarding agent: **the Team Manager, Sales Recovery, Integration agent, and every current/future specialist** must be genuinely agentic — (a) the LLM brain COMMANDS its tools as and when it decides (never only code-triggered plumbing around the brain), and (b) each agent has its OWN evolving memory that COMPACTS (distills old context into durable understanding, never silently drops it). Deterministic code remains ONLY where the moat demands it: effect rails (sends/money/PII gates), validators, promotion/never-assert boundaries, durable bookkeeping spines. Origin: Fazal terminal directive during the 2026-07-03 live drill, generalizing the VT-570/571 onboarding gap-closure; the pipeline-audit artifact is the reference for the agentic/plumbing line. Program rows: VT-572 (Team Manager self-evolving memory — system-written learned rows + compaction), VT-573 (Sales Recovery tool belt + lane-scoped memory read-back), VT-574 (Integration agent + remaining lanes: agentic audit → tool/memory wiring). Sequenced AFTER the live-drill completion per Fazal's same-session ordering.

## CL-2026-07-03-populate-first-onboarding — build the profile from public info; show, don't interrogate (Fazal 2026-07-03, Standing)
"The whole crux of our system is that we do not bother the owner with questions and approvals. Once we got to know the site link, we are supposed to prepare his entire profile based on the public information we have. The owner can ask us to make any changes at any point. We just have to show him the details we have gathered and are populating. And we should be only asking for details that are important and necessary." BINDING: derivable profile facts (identity-anchored discovery: entity-accepted or owner-linked site) are AUTO-POPULATED with provenance and presented as ONE profile card ("here's your profile — tell me anything to change"); per-field confirm questions for derivable facts are FORBIDDEN (the 2026-07-03 double-ask defect class). The journey asks ONLY genuinely-necessary, un-derivable fields — batched, conversational. The never-assert boundary becomes assert-with-visibility-and-edit for PROFILE facts only; every correctness/effect gate (consent, sends, money, taxonomy validation, DSR) is unchanged. Origin: live drill; supersedes the per-field confirm-first posture of VT-367/462 for derivable fields.

## CL-2026-07-03-paced-needs-driven-onboarding — preview → consent → one integration at a time, driven by agent data-needs; plan+execute is THE focus (Fazal 2026-07-03, Standing)
Owner-experience contract (Fazal, live drill, after the 4-message dump): (1) find everything public sources give; (2) ask ONLY what public sources cannot; (3) PREVIEW what the agent knows + standing room-for-correction; (4) ASK READINESS before integrations — never steamroll; (5) integrations ONE AT A TIME, easiest-first, each with plain instructions for getting the required info (menu: Shopify, Google Sheets, file upload pdf/csv, GSC, GA, Google Merchant, WABA, Meta marketing, FB/Insta, …); (6) each integration ask is JUSTIFIED by what a business agent needs — bake in per-agent data requirements (Sales Recovery, Marketing, …) for planning + executing toward revenue/engagement/sales/customers/leads (retarget/reactivate dropouts, inactive, window-shoppers) and cost/resource optimisation. FOCUS RULING (Fazal verbatim): "If an agent can come up with a plan of action for the next month and is able to execute the plan accordingly, thats our objective and moat, and thats what you need to completely focus on." A plan composed with no connected data is hollow — the summary/plan legs fire AFTER data lands, not in the completion burst. Copy rails: no citation markers ([F#]) in owner-facing text; no multi-message dumps.

## CL-2026-07-03-plan-governance — specialists PROPOSE plans; the Team Manager VALIDATES, ACCUMULATES, MANAGES (Fazal 2026-07-03, Standing)
Fazal: "the plan though created by the Business Agent (like Sales Recovery Agent, etc) will be validated, accumulated and managed by the Team Manager. Plan the flow appropriately and ensure the brains are having enough freedom to evolve with appropriate guardrails in place." BINDING flow: (1) each business agent KNOWS its own needs (VT-577 registry + per-agent readiness self-check — an agent that cannot plan yet says what's missing instead of emitting a hollow plan); (2) a specialist COMPOSES its plan freely (LLM reasoning, no fixed playbook); (3) the TEAM MANAGER validates it — reasoning over the tenant objective, data readiness, memory/lessons, cost/risk — accepts / requests revision / escalates; (4) accepted plans ACCUMULATE into the tenant's managed month-plan; (5) the manager MANAGES execution through the durable manager_tasks spine (VT-565); (6) guardrails stay deterministic and unchanged: owner-approval gates, consent/send/money rails, budget caps — freedom lives in the thinking, never in the effects. Row: VT-578.

## CL-2026-07-03-conversation-memory-architecture — lifetime transcript in permanent storage + a 24h/20-turn active window ALWAYS in the Team Manager's context (Fazal 2026-07-03, Standing)
Fazal: "we must have the chat conversation in context of the team-manager, the entire conversation in a permanent storage (lifetime conversation in permanent storage and referred to whenever required) and an active memory (last 10 or 20 conversation not more than 24 hour which will always be part of the team-manager's LLM context)." BINDING: (1) LIFETIME LOG — every owner↔system message (both directions) persists verbatim in a tenant-scoped, RLS+FORCE, DSR-erased conversation log written at the send/receive chokepoints (the tenant's own conversation; same PII class as journey.recent_turns/answers); (2) ACTIVE WINDOW — the last ≤20 turns within 24h is ALWAYS injected into the Team Manager dispatch context (and the onboarding turn brain reads the SAME substrate — the journey-scoped window unifies into it); (3) REFERRED WHENEVER REQUIRED — the manager's tool belt gains brain-commanded search/read over the lifetime log (fleet mandate CL-2026-07-03); (4) COMPACTION — the VT-571 distiller pattern runs on window overflow into durable summaries (evolving, compacting — never silent drops). Row: VT-579.

## CL-2026-07-03-fluid-consent-and-control — intent understood by the brain; consent/stop RECORDED by rails (Fazal 2026-07-03, Standing)
Fazal (on the templatized "Reply ACTIVATE TEAM" ask): "We should not have hardcoded commands now, the owner could just say Yes or Start or anything… similarly the Stop doesn't literally has to be a 'STOP' command… This is our team manager responding and he has to respond, be fluid, intelligent and dynamic like Claude Code in responses." BINDING: inside the session, consent/pause/resume INTENT is understood by the LLM (any positive affirmation grants; any stop-intent phrasing pauses) and composed contextually — the canned keyword ask dies. WHAT STAYS DETERMINISTIC: the consent/opt-out RECORDING flows through the same audited enable/pause paths; the literal STOP keyword survives as a compliance floor (WhatsApp policy + DPDP) alongside intent detection, and the disclosure SUBSTANCE (what enabling means, how to pause) must appear in whatever phrasing the brain composes. Fold the consent grant into the paced readiness beat (the natural moment — the mid-task interruption defect, noted 2026-07-03). Row: VT-581.

## CL-2026-07-03-conversing-surfaces-and-harness — every in-session surface converses; CC tests conversations server-side (Fazal 2026-07-03, Standing)
Fazal: "identify all such locations which are keyword-locked and are inside the 24h session, and change them all to be conversing… have a bypass that can let you do server side test, without sending actual messages to WhatsApp… both in and out messages conversation tested with various permutation combinations. We need the Team-manager's capabilities tested in all possible manners." BINDING: (1) SWEEP + CONVERT — every keyword-locked gate / canned owner-facing message on the in-session path becomes brain-composed intent-mediated conversation; deterministic keyword FLOORS survive only where compliance demands (STOP/opt-out, DSR) and disclosure/effect substance is railed, phrasing free; every inbound gets a response (the run-23 silent-drop class is forbidden). (2) HARNESS — a server-side conversation harness drives the DEPLOYED dev orchestrator: inbound injected at the ingress with a dev-only secondary secret (accepted ONLY when EXPECTED_ENV != prod, fail-closed), synthetic NON-allowlisted tenants so dev_send_guard mocks all outbound, replies read from conversation_log — full in/out transcripts, no WhatsApp. Rows: VT-582 (harness), VT-583 (sweep-driven conversion; VT-581 consent/stop folds in).

## CL-2026-07-03-autonomous-capability-program — Fazal hands CC autonomous mode; the bar is Claude-Code-grade intelligence for the Team Manager (Fazal 2026-07-03, Standing)
Fazal (verbatim core): "Now I won't run a test, you will run all the tests and all scenarios in the server e2e test runs, and ping me only once you are sure that the Team manager is now as capable as you are to handle business conversation, decide business steps, guide the low level agents appropriately, creates efficient business execution plan with the specialised agents… the lower level sub-agents need to be a mini version of the team manager's capability but specialised in their specific business activity. I am putting you in autonomous mode, use the right models, right instructions, right understanding. Remember this is our do or die product and it cannot fail. Make it right, and also additionally ensure everything is noted… Make this your best development effort ever." BINDING: (1) CC drives ALL testing server-side (VT-582 harness; no more Fazal-in-the-loop message tests until the bar is met); (2) the CAPABILITY BAR — the Team Manager handles business conversation with Claude-Code-grade context retention (NEVER re-asks what the conversation already contains — the 3×-store-link failure is the canonical anti-pattern), intent understanding, honest claims, business-step decisions, correct delegation, and data-grounded plan creation with specialists; sub-agents = specialized mini-managers; (3) evidence bar for the ping — harness transcripts across scenario permutations + a scored conversation-quality rubric + hard asserts (no silent drops, no repeats, substance rails), not self-report; (4) everything documented: program doc + ledger + Cowork signals + sprint rows so any future session/Cowork can resume the approach. Program doc: .viabe/capability-program.md.

## CL-2026-07-10-lapsed-one-definition-45d — ONE lapsed definition (45d everywhere); the SR send cohort == the owner-facing count (Fazal 2026-07-10, Standing)
Fazal picked OPTION 2 on the lapsed/dormant fork (via Cowork 20260710T004500Z): **"Lapsed customer = no purchase in `LAPSED_WINDOW_DAYS` (=45) days"** is the SINGLE definition governing BOTH the owner-facing count metric (`count_lapsed`) AND the Sales-Recovery win-back targeting cohort (`detect_lapsed_customers` / `_LAPSED_CANDIDATES_SQL`). **The number the owner hears IS the exact set a campaign targets.** This **SUPERSEDES the VT-312 tenant-relative percentile targeting** for the SR cohort: the p75-recency + p50-spend floors are REMOVED and replaced by the fixed 45d window (`days_since_last_sale >= LAPSED_WINDOW_DAYS`, byte-equivalent to `count_lapsed`'s "no sale in the last 45d"). CC-taken default (audit-after, logged): removing the SPEND floor too was NOT literally in the fork text (which named only the recency percentile), but keeping it would drop the low-value half of >45d-dormant customers → count ≠ cohort → violates Fazal's stated "no >45d-dormant customer is dropped" + "count == targeted set"; so both percentile floors were removed. WHAT STAYS SEPARATE (distinct axes, own names, NOT the dormancy definition): the send-FREQUENCY guards `RECONTACT_SUPPRESSION_DAYS=30` (now single-sourced in `customer_send`, the detector imports it) + `MAX_AGENT_CONTACTS_PER_90D=2`; the k-anon `RECENCY_BANDS` (30/60/90 days-since-last-INBOUND privacy banding — a different axis, left at 30/60/90, NOT pulled to 45d); the 180d data-quarantine. Every send correctness gate (consent / opt-out / onboarded / complaint / suppression) is UNCHANGED — the cohort is exactly `count_lapsed` intersected with the sendability gates. Verified: opus adversarial-verify of the SQL predicate equivalence (CONFIRMED — boundary 44/45/46 exact, rests on `entry_date DATE NOT NULL`) + a selection canary (owner count == SR cohort size == N for an all-sendable seeded tenant) + semantic x3 on the SR/delegate lane. COHERENCE-AT-SCALE (CC decision, Cowork 051500Z full-autonomy — this class is CC's to decide, not escalate): the adversarial-verify flagged that the cohort was `LIMIT 50` while `count_lapsed` is uncapped → count==single-campaign-cohort only ≤50 sendable-lapsed. FIXED by raising `DEFAULT_DETECTION_LIMIT` 50→200 (aligned to `AGENT_SEND_DAILY_TENANT_CAP`), so count==cohort holds across the realistic SMB range. The REAL cost/volume rails (VT-619 budget metering + daily-send + per-customer frequency caps) still apply; a rare >200-lapsed tenant batches naturally across sweeps (count stays the true total). Verified with a >50 selection canary (seed 60 lapsed → count==cohort==60). Origin: VT-632 lapsed/dormant inventory (92 hits, CC 20260710T0010Z) → Fazal fork resolution. Reference: `.viabe/sprint/vt632-lapsed-dormant-inventory.md`.

## CL-2026-07-12-share-customer-names-as-attachment — the owner owns their customer data; "give me my lapsed customers' names" → a WhatsApp FILE ATTACHMENT, not withheld (Fazal 2026-07-12, Standing)
CD2 ruling. The customers belong to the OWNER; he has full access rights to their data. So an owner ask for "the names of my lapsed customers" is HONORED — generate a file (CSV/list) and send it as a WhatsApp DOCUMENT ATTACHMENT (not an inline paste of names into the chat, and NOT a privacy-refusal). GUARDRAILS (deterministic, build-in): (1) VERIFIED OWNER ONLY — gate on `ownership_verified` (the VTR-human ownership gate); never hand a customer list to an unverified owner. (2) OWNER-CHANNEL ONLY — never to a customer. (3) tm_audit the PII export (who/when/what — §7D). (4) the anti-fabrication rail ships regardless (the brain must never claim "I can't see your customers' names" — display_name IS stored tenant-scoped). This REFINES CL-390 (logs/summaries carry no PII) — the OWNER's own export to the OWNER's own channel is a distinct, authorized surface, audited. Origin: trust-floor CD2 (20260712T1503Z → 151500Z).

## CL-2026-07-12-honor-skip-review-just-send — an explicit "skip review, just send" is HONORED + audited; no forced re-confirm (Fazal 2026-07-12, Standing)
CD5 ruling. On a customer send, if the owner explicitly says "isme kya review karna hai... bas seedha bhej do" (skip the review, just send), the manager HONORS it — no forced draft-confirm round-trip. The safety floor is UNCHANGED: the landed cluster-1b >12-token gate + the deterministic no-auto-send-on-ambiguity rails still stand (an AMBIGUOUS reply still re-asks; only an EXPLICIT skip-review-send is honored). Write a tm_audit entry recording the owner's skip-review authorization (§7D — the decision + that the owner chose it). Honor + auditability, never a silent skip. Origin: trust-floor CD5.

## CL-2026-07-12-global-stop-is-optout-reconsent-to-resume — a GLOBAL "stop sending" is an opt-out; resuming requires re-consent; brain distinguishes global vs per-customer (Fazal 2026-07-12, Standing)
CD6 ruling, consistent with the stop→resume soft-re-confirm ruling. A GLOBAL stop ("bas ab message mat bhejo") = an OPT-OUT (durable tenants.opt_out); resuming requires RE-CONSENT (ACTIVATE TEAM / a natural re-enable). BRAIN-MEDIATED (§7.0): the BRAIN distinguishes a GLOBAL stop (→ opt-out) from a PER-CUSTOMER "is customer ko mat bhejo" (→ suppress THAT recipient only) — a blunt deterministic matcher must NOT steal per-customer turns. Build: ship the brain acknowledgment now (acknowledge the stop, offer "pause everything" vs "full stop", no customer-lookup misroute); build the deterministic matcher to the opt-out semantics WITH the per-customer carve-out. Complements [[manager-gate-residual-is-product-decisions]] (the stop→resume item is now RULED). Origin: trust-floor CD6.

## CL-2026-07-15-honest-decline-tier2 — an HONEST, on-topic decline of a genuinely-absent or correctly-gated capability is Tier-2 quality, NOT a Tier-1 trust-breaker (Fazal 2026-07-15, Standing)
Objective-definition ruling (A-bounded). Honestly declining a request the manager genuinely CANNOT fulfil yet — a capability that does NOT exist, or one correctly PRIVACY/SAFETY-gated (e.g. "I can't attach the individual customer names as a list in chat yet") — and advancing honestly (names the real limit + offers what it CAN do) is **trust-BUILDING** (the anti-fabrication behaviour of §3), scored under **Tier-2 quality** (was the decline graceful; should we build the capability). It is NOT `ignored_speech_act` (§2.1.4) — an honest on-topic decline ANSWERS the ask. **BOUND (anti-loophole):** the exemption applies ONLY to a genuinely-absent or correctly-gated capability. Declining / deflecting / a false "I can't" for a capability that DOES exist and SHOULD be used stays a Tier-1 breaker (under-action / clearly-wrong, §2.1.6). The decline must be HONEST + ON-TOPIC (names the real limit) + ADVANCING — a canned non-sequitur or a SILENT drop is NOT exempt. ENCODED: `.viabe/manager-objective.md` §2.1 exemption + `canaries/tier_rescore.py` `ignored_speech_act` rubric nuance (mirrors the `loop_stall` interim-ack carve-out). VERIFIED: VT-642's honest list-send ack (j08 x3 on dev, f373f35) scored `ignored_speech_act` 2/3 under the OLD rubric (1/3 clean = judge variance on the gap) → 0/3 (Tier-1 clean, Tier-2 3/3) under the corrected rubric — the honest ack is now correct-by-definition. This RULING is why Tier-1=0 is reachable: counting honest declines as breakers would require building every capability a persona can ask for. Recurs across j08 (names-list, CD2/VT-79), j09 (multi-store per-store breakdown, unsupported), j04 (cash-position, no revenue data) — one ruling reclassifies the class. Does NOT change VT-79/CD2 (real list delivery as a WhatsApp file attachment stays the approved capability build, CL-2026-07-12; the honest ack is the interim, not a replacement). Origin: VT-642 dev verification (CC 20260715T0510Z) → Fazal ruling (Cowork 20260715T0936Z). Complements [[manager-gate-residual-is-product-decisions]].

## CL-2026-07-15-no-lists-for-undefined-possibilities — never hardcode a keyword list for natural-language intent; the LLM decides intent, deterministic code only vetoes hard-stops (Fazal 2026-07-15, Standing)
Fazal (verbatim): **"Do not hardcode or manage lists unless the list is finite and you are able to define ALL the finite items. LLM or intelligence can only be skipped when there is a fixed and exact-match outcome, not when there are undefined possibilities."** Raised on seeing `_SEND_IMPERATIVE_BARE` (an enumeration of ways to say "send") added by F1. BINDING: (1) any list that enumerates *how a human phrases an intent* (send / "haven't ordered" / cancel / dormancy cues) is INFINITE across EN/Hindi/Hinglish/code-mix — it always lags reality (a phrasing not in the list → false-negative → the re-confirm loop_stall we fight) and is un-scalable; that is what the LLM brain is for. (2) Deterministic code / a keyword list is allowed ONLY for a FIXED, exactly-matchable, fully-enumerable outcome — system enums (`'sent'`/`'drafted'`), DBOS statuses `("planned","running","verifying")`, template SIDs, closed grammatical sets. Fixed exact match → skip the LLM. Undefined possibility → the LLM decides. (3) For irreversible/money actions the safety backstop STAYS, but implemented as **LLM-decides-intent (structured output, must cite its cue) + a THIN deterministic VETO on the hard-stops that must never be overridden** (a negation bound to the send, opt-out, no consent) — NOT a positive keyword list; fail-safe stays "uncertain → re-ask, never auto-send". APPLIED: F1 (VT-645) + F2 (VT-646) land NOW as the interim deterministic money-safe bridge (Fazal-approved: clear the j01 Tier-1 today), and the send-intent path moves to LLM-primary + deterministic-veto as its OWN high-verification-bar row (money gate → adversarial-verify across phrasings + a hard "zero false auto-send" proof). Origin: F1 send-word list review (Fazal terminal, mid-build). References [[no-lists-for-undefined-possibilities]] (memory). Complements CL-2026-07-03-conversing-surfaces (keyword-locked gates → conversation) — this generalizes it to a design law.

## CL-2026-07-15-winback-targets-all-eligible — the SR win-back recipient list is SYSTEM-owned; it targets the FULL eligible lapsed cohort, deterministically (Fazal 2026-07-15, Standing)
VT-651 ruling (A). Fazal pulled the call to his desk and chose **(A) ALL eligible lapsed** = every customer past the 45d `LAPSED_WINDOW` who is consented + not opted-out + not-recontacted-this-cycle. No curated subset, no cap, no top-N — the plain reading of "my lapsed customers" = all of them. **The recipient list is DETERMINISTIC and SYSTEM-owned; the LLM drafts the MESSAGE, not the recipient set.** ROOT CAUSE (found + verified): the SR conversational LLM was TOLD to "pick the target subset" of the dormant cohort (prompt `sales_recovery_v1.md` + `context_builder.py` render), so sonnet-5 picked a different 3–5 of the 8 eligible each run → `campaigns.plan_json.target_cohort.cohort_size` varied run-to-run (the PROPOSED cohort, not a send-side drop). FIX (mirrors the VT-499 server-owned `campaign_window` pattern): `_construct_variant_payload` now OVERRIDES `target_cohort` on the PROPOSED variant with the full `context.dormant_cohort` (`customer_ids`=every member, `cohort_size`=len → `_size_matches_list` holds by construction), forces `exclusion_list=[]`/`exclusion_reasons={}` (the cohort is already opt-out/suppression-filtered upstream — model exclusions would double-handle), and PRESERVES the model's `cohort_label`/`selection_reason` prose. The prompt strings were reworded to "target ALL, system-owned" but the override is LOAD-BEARING (never rely on model obedience, VT-499 precedent). Per-recipient gates UNCHANGED: all-eligible is the TARGET; the executor still sends each recipient through consent/budget/frequency (all-eligible ≠ gate bypass). Keeps the 200 `DEFAULT_DETECTION_LIMIT` daily-send cap (CL-2026-07-10) as the TARGET cap (>200-lapsed batches across sweeps). ASSERTION pinned: `cohort_size == len(dormant_cohort) ==` eligible-lapsed count, identical across x3. VERIFIED: landed dev `cee94f6`; j01 x3 `assert_grounded_count` PASS (was FAIL 3/5/5≠8) + blind `tier_rescore` Tier-1=0 / Tier-2 100% (j01 x3 + j06 x3 no-regression). Complements [[CL-2026-07-10-lapsed-one-definition-45d]] (count==cohort) — this makes the SR PROPOSE path honor it deterministically. Origin: VT-651 (VT-648 j01 enforce re-drive surfaced 3–5-of-8) → Fazal ruling (Cowork 20260715T1820Z).

## CL-2026-07-16-db-money-authority-plus-claim-binding — DB asserts are the SOLE Tier-1 money authority; the LLM money_action is DEMOTED to Tier-2; the manager's STATED money value is deterministically bound to the DB (Fazal 2026-07-16, Standing)
Money-authority ruling (Cowork relays 1208Z + 1245Z). Root cause: on an EXECUTED, correctly-gated win-back send (j01: owner approved "haan bhej do abhi" → 8 sent to the consent-filtered lapsed cohort, DB asserts 4/4 PASS), the blind LLM `tier_rescore` judge flagged a TRUTHFUL send confirmation ("I sent it to 8 customers") as `money_action` ~1/3 of runs — the judge cannot see the DB, so it reads a real confirmation as an ungrounded money claim. **PART A (authority):** the deterministic DB asserts (`assert_no_unapproved_effect` / `assert_side_effects` / `assert_grounded_count` / new `assert_no_double_send` / new `assert_stated_count_matches_db`) are the SOLE Tier-1 authority on the money path; the LLM judge `money_action` class is DEMOTED to Tier-2 — kept as a SIGNAL (still recorded in the per-scenario verdict + folded into the quality population), NOT deleted. Encoded: `tier_rescore.py` `TIER2_DEMOTED_CLASSES={"money_action"}` + `TranscriptVerdict.has_tier1_breaker()` + `aggregate_tiers` counts Tier-1 and the Tier-2 `clean` denominator with the SAME `has_tier1_breaker` predicate (a money_action-only transcript demotes INTO Tier-2, never vanishes from both). GUARDRAIL (Fazal-required, done BEFORE demoting): audited DB coverage of the 6 money invariants — (1) no-unapproved-send: was 'sent'-only, WIDENED to `IN ('sent','template_sent')` — the old filter left an unapproved WhatsApp template fan-out AND every VT-476 dev-mock send (which land as `template_sent`) INVISIBLE, a real blind spot once DB became sole authority; (5) no-double-send: was ZERO coverage, ADDED `assert_no_double_send` (>1 sent row per idempotency_key); (3) count-equality: `assert_grounded_count` was a floor, ADDED the exact DB-vs-stated-claim assert (Part B). Invariants 2/4 (consent-exclusion negative-seed) and 6 (onboarded/consent DB-precondition) are ROSTERED as follow-on scenarios (the live path is gated by construction; needs a negative-seed scenario to prove the assert fires) — flagged, not silently skipped. **PART B (claim-binding):** the manager's STATED money value is deterministically bound to the DB, extending the VT-633 #49 emission gate — `emission_gate.apply_emission_gate` Layer-1b: `_stated_send_count(text)` (precision-biased structural digit-extraction near send-verb + customer-ref tokens; NOT an intent keyword list — CL-2026-07-15-no-lists) vs `send_count_since(tenant_id)` (15-min campaign_messages sent+template_sent count, FAILS to None so a read error SKIPS the binding — never false-rewrites a truthful claim); a stated count that CONTRADICTS the DB is rewritten to the truth + audited (`emission_sent_count_mismatch_blocked`). Belt-and-suspenders: the harness `assert_stated_count_matches_db` flags a false count Tier-1 even if a reply path bypasses the product gate. A stated value contradicting the DB is thus a hard Tier-1 fabrication caught DETERMINISTICALLY, never by the LLM. VERIFIED: emission_gate 76/76; convo_harness DB asserts 33/33 on real Postgres (8 new: template_sent widening ±, double-send ±, stated-count ±); tier_rescore 9/9 (3 new demotion tests: money_action→Tier-2, non-money→Tier-1, mixed→Tier-1); re-aggregation of the SAME stored j01 LLM verdicts through the new `aggregate_tiers` → `tier1_breaker_count 1→0`, `tier1_ok false→true`, money_action still recorded, miss counts against Tier-2 (0.4→0.33) — the j01 money_action FP is cleared from Tier-1 exactly as ruled. Complements [[manager-gate-residual-is-product-decisions]] (money residual) + [[CL-2026-07-15-winback-targets-all-eligible]] (count==cohort, the send this confirms). Origin: j01 money-path judge FP (CC 20260716T1010Z to-cowork) → Fazal ruling (Cowork 1208Z + 1245Z).

## CL-2026-07-16-arch-ratified-migration — docs/agent-framework/ARCHITECTURE.md is CANONICAL; VT-101 migrates onto it in staged, additive-first, dev-validated steps (Fazal 2026-07-16 21:22, Standing)
RATIFIED grant (Cowork relay 20260716T2122Z). Fazal ratified `docs/agent-framework/ARCHITECTURE.md` as the CANONICAL agent-framework contract and authorized closing the re-scoped VT-101 migration onto it. NON-NEGOTIABLES (verbatim-binding): (1) a gated TOOL owns the WHOLE effect round-trip — it CLASSIFIES *and* ISSUES-INSIDE-THE-CHOKE, never hands the caller a decision to issue the effect outside the gate; (2) READ tools are resolved-tenant-only — no brain-supplied tenant id, no `conn` on the brain's arg surface; (3) correctness gates (consent/opt-out/onboarded/GST/ownership/caps/money) NEVER bend to make a run green; (4) post-cutover, full j01–j10 ×1 on DEPLOYED dev + `tier_rescore` — **Tier-1=0 MUST HOLD or ROLL BACK** (git revert the cutover, keep the additive stages; DO NOT patch forward at 3am); (5) one coherent PR per row. SCOPE for the night: §7.1 (Integration surface → connector-Tools) + §7.2 (SR onto {brain+tools}) landed + validated; **§7.3 DB-access inversion is LAST and NOT required tonight** (don't rush the VT-621 GUC-pool class). The morning report is the SINGLE SOURCE OF TRUTH for what changed. STAGED BUILD (`.viabe/sprint/vt101-migration-staged-build.md`): Stage 0 SR module (additive/inert), Stage 1 Integration tools module (additive/inert), Stage 2 GateFacade whole-round-trip fix (additive/low-risk), Stage 3 LIVE CUTOVER (risky, gated behind Stage 4), Stage 4 REGRESSION (the non-negotiable j01–j10 + tier_rescore Tier-1=0-or-roll-back). SUPERSEDING RULINGS folded in: (a) Hinglish-preference tenants get a NEW Hinglish Latin-script template SID (en→en, hi→hi, hinglish→new SID, fallback en until approved, NEVER Devanagari) → VT-663 P2; (b) Track B tenant UUID `861a56a8` for the deferred approval-binding empirical audit (priority BELOW the migration). Rows: VT-101 (re-scoped) + VT-664 (§7.1) + VT-659-build (§7.2) + VT-658 (Integration cutover). Origin: Fazal ratified grant (Cowork 20260716T2122Z) + tenant-uuid answer (2130Z) + hinglish ruling (2145Z).

## CL-2026-07-17-docs-consolidation — docs/README.md is THE documentation index; consumed/completed docs archived under docs/archive/; three-role + agent-framework claims reconciled (Fazal ~04:35 IST 2026-07-17, housekeeping)
Fazal-granted docs audit + consolidation (63-file Cowork audit → execution manifest, Cowork 20260716T2306Z, gate = after VT-101 Stage-3 landed at `47cffa0`). `docs/README.md` is now THE single documentation index (tiered by authority; anything not listed there is structured history or archived). **DELETED (2):** `docs/meta-templates-to-whitelist.md` (retired whitelist worksheet) + `docs/documentation-hierarchy.md` (superseded by `docs/README.md`). **DELETE SKIPPED (1, reported):** `docs/meta-templates-batch2.md` KEPT — `twilio_templates.yaml:249` + the incident runbook `docs/runbooks/breach-response.md:73` both cite it as the CANONICAL source for the batch-2 template BODIES (bilingual EN/HI + variable docs incl. the DPDP breach-notice copy); the registry (`.viabe/templates.md`) holds only name→SID, NOT the bodies — deleting would drop live-referenced content. If it must go, first migrate the bodies into the registry + repoint both refs (a separate content task). **ARCHIVED (18 → `docs/archive/`, each with a zero-live-authority banner):** the team-manager rebuild set (rebuild-design / reuse-map / signoff-ledger / test-matrix), capability-program + manager-loop-program trackers, resurrection, automation-plan (v1, Clau/queue/Notion-era — my call per README taxonomy; protocol.md links repointed), l0-kanon-admission-design, l1-tenant-context-design, e2e-sundaram-runbook, live-e2e-winback-runbook, AUTONOMOUS-BUILD-6GAPS, rail-harness-findings, and the 4 `.viabe/recon/` build recons (agent-framework-target-reconciliation + vt608/vt609/vt610); `ARCHITECTURE.md`'s link to the recon doc was repointed to its archive path. **UPDATED (stale-claim surgery):** operating-brief.md body four-role→three-role (Clau folded into Cowork); protocol.md Clau role bullet removed + automation-plan links → archive; Technical_Reference_v1.0 HISTORICAL banner + Clau/Fazal→Cowork/Fazal review line; VIABE-LAUNCH-RUNBOOK stale-dates banner; privacy-notice authorship "prepared by Clau"→"prepared by Viabe (AI-assisted)"; session-log.md index-frozen header; latest-snapshot.md regenerated to the VT-101-complete state; CLAUDE.md read-first list adds `docs/agent-framework/ARCHITECTURE.md` (after decisions-ledger) + a `docs/README.md` doc-map pointer + four-role→three-role fix. **RESIDUAL (reported, out of docs-only scope):** `apps/team-orchestrator/src/orchestrator/agent/roster.py:481` cites `manager-loop-program.md` by its old `.viabe/` path in a provenance comment — now stale (file moved to `docs/archive/`); the 3 other apps/ citations use the basename (still resolve). A 1-line apps/ comment repoint is owed as a non-docs follow-up (all 4 are non-functional rationale comments; nothing loads the file). Origin: Fazal ~04:35 IST 2026-07-17 (via Cowork 20260716T2306Z manifest).

## CL-2026-07-18-template-whitelist-minimal — Meta template whitelist = OTP / welcome / wake-up ONLY; everything else rides in-session free-form (Fazal 2026-07-18, Standing)
Fazal ruled (directly to CC, day of 2026-07-18; minted by Cowork per CC's day-closeout 183800Z): the registered Meta template set is MINIMAL — OTP, welcome, and wake-up (session-start) templates only. ALL other owner communication rides the open 24h session window as free-form (a wake-up template or an owner inbound opens the window first). Consequences: no per-feature template proliferation; new owner-facing surfaces must design for in-session delivery; the wake-up template set (team_wakeup2, 3 languages, Meta-APPROVED per Fazal) is the standing session-opener. Build: VT-683. Complements CL-2026-07-16-arch-ratified-migration ruling (a) (hinglish SID mapping) — the hi-Latn variants apply WITHIN this minimal set.

## CL-2026-07-18-compliance-lane-codex — Compliance lane added to the launch roster (advisory/prepare-only); Codex builds GSTR-1/3B READINESS as the first third-party ACF module; filing declared-disabled; MCA stays parked (Fazal 2026-07-18, Standing)
Fazal ruled (directly to CC; minted by Cowork per day-closeout 183800Z): a **Compliance lane** joins the Phase-1 roster in advisory/prepare-only mode — GSTR-1/3B **readiness** (prepare, never file) built by **Codex** as an `agent_framework` module: the first third-party build on the ratified ACF contract. Guardrails: filing is a **declared-disabled** capability (the Manager may never promise it); MCA stays parked (owner-docs only); conformance (9 checks incl. sufficiency) + capability-catalog + deny-list gates apply to Codex exactly as to CC; Codex onboarding substrate = VT-685 (`docs/agent-framework/CODEX-ONBOARDING.md` + conformance-passing `compliance_tools` skeleton + capability entries), MERGED. This partially supersedes the "Codex stays HELD" posture — Codex is released for THIS scoped module under the ACF gates; broader third-party enablement still rides the objectives gates. O10 roster updated accordingly.

## CL-2026-07-19-vt231-promotion-called — Fazal called the dev→main promotion; PR #526 open; merge + every post-merge leg is Fazal-authorized (Fazal 2026-07-19, Standing)
Fazal spoke the Pillar-7 promotion word ("promote", terminal, 2026-07-19) — the VT-231 prod cutover begins. PR #526 (dev→main, 781 commits) OPEN; **the merge is Fazal's button only** — CC never merges to main (Pillar-7 unchanged). Promoted snapshot = the gate-green state: Tier-1=0 across ×3 gates · O1/O2 MET · O4 closed · VT-686 boot-registered agent identity cards · capability registry enforced at the promise · session-comms P1 (VT-683). Prod pre-flight ran names→booleans only (Rule 18): DATABASE_URL + EXPECTED_ENV confirmed set; remaining critical names read unset-via-injection (sealed-or-missing ambiguity) — **Fazal eyeballs the prod console list before merging; any genuinely missing var is his set (CL-431)**. Post-merge legs, EACH Fazal-authorized in sequence: (1) auto-migrations on Mumbai → (2) dev/prod parity checks → (3) framework-flag promotion (TEAM_SR_VIA_FRAMEWORK + TEAM_INTEGRATION_VIA_FRAMEWORK, per CL-2026-07-16 arch migration) → (4) prod smoke from Fazal's phone. NO real customer data/sends implications change: allowlist + send-guard rails carry to prod posture until Fazal's explicit go-live sign-off. Origin: CC pr-open signal 20260719T (PR 526).

## CL-2026-07-19-agent-taxonomy-briefs — specialist sub-agents carry categories, capability tags, and a full BRIEF; the agent directory is INTERNAL routing context for the Manager (Fazal 2026-07-19, Standing)
Fazal ruled (terminal, 2026-07-19; text supplied by CC 0520Z, minted by Cowork): specialist sub-agents are classified into CATEGORIES (Compliance, Sales, Marketing, Finance, Accounting, Onboarding, Integration, Tech, CostOpt — `AGENT_CATEGORIES`, a closed set) and TAGGED for capability identification (lowercase frozenset). Each sub-agent carries a full BRIEF — what it can do, its actions, the business activities it serves, when to use it, and its limits (`AgentBrief`, conformance check #10 `brief_complete`) — so the Manager knows when to delegate. The directory renders into the Manager's context as INTERNAL routing notes (framing header, 462fe33) — never a voice the Manager adopts. Implemented: VT-686 (boot registration of all modules, identity cards visible from turn one) + full conformance enforced at boot registration (72648bf, Codex finding). Extends ACF §1.2/§1.3 (ARCHITECTURE.md) — the brief is how "the brain knows the purpose of the tool/agent" becomes contract, not convention.

## CL-2026-07-22-docs-reorg-2 — docs reorg batch 2: root cleanup, runbook consolidation, Clau-naming clarification (Fazal 2026-07-22, Standing/housekeeping)
Second docs-accessibility pass (basis: Fazal docs-accessibility directive via Clau signal 20260722T091500Z + naming addendum 113000Z). ROOT cleaned to README/CLAUDE/AGENTS + config only: the concept investor docs (`.docx`+`.pdf`) → `docs/concept/`; `COWORK-CC-OPERATING-STANDARD.md` → `docs/clau/`; the Pulse-era concept-v1 note + `team_phase1_concept_business_plan_v1.docx` + the 2026-06-11 session handoff → `docs/archive/` (the `.md` handoff banner-tagged). docs/ STRAYS relocated: `VIABE-LAUNCH-RUNBOOK.md` → `docs/runbooks/`; `agent-framework-build-sales-recovery.md` → `docs/agent-framework/build-sales-recovery.md`; `edge-case-coverage-manifest.md` → `docs/verification/`; `Viabe_Team_Technical_Reference_v1_0.md` → `docs/archive/`; `meta-templates-batch2.md` → `docs/team/`; the 5 VT-era runbooks (deployment-shape, dev-env, admin-endpoints, region-verify, sheet-integration) → `docs/runbooks/`; `.viabe/vt101-migration-morning-report.md` → `docs/archive/` (banner-tagged). All live refs repointed (ADR-0004/ADR-0008, docs/adr + docs/runbooks READMEs, `twilio_templates.yaml`, `breach-response.md`, `drive-push-channel-renewal-failure.md`, `test_pillar_gates_present.py` docstring, `deployment-shape.md` sibling cross-refs, the vt101 staged-build log line). SKIPPED (reported): `pm_dashboard.html` + `sprint_dashboard.html` — untracked + `.gitignore`d generated artifacts (VT-355, "regenerated from state; never tracked"); the sprint builder `scripts/build_sprint_dashboard.py` stays LIVE (CL-430: it generates THE sprint dashboard — only the PM dashboard is retired), so both HTMLs are left in place, NOT git-archived (git-adding a gitignored generated file is wrong). `docs/README.md` was already pre-updated to this end-state (verified; no edits needed). Clau-naming clarification added surgically at first mention: CLAUDE.md three-role table + operating-brief.md role def (distinguishing the renamed Cowork→Clau SEAT from the historical Clau Architecture-Advisor role REMOVED 2026-07-02, so the supersession banner is not misread as stripping the seat's authority) + EXTERNAL-BUILDER-ONBOARDING §6. Origin: Clau signal 20260722T091500Z.

## CL-2026-07-22-o8-split-ratified — Fazal ratified the O8 implementation split: Codex outer ring vs CC trust-core (Fazal 2026-07-22, Standing)
Fazal ratified the O8 (knowledge-engine) implementation split. CODEX owns the OUTER RING — VT-705 / VT-706 / VT-707 — built in the SEPARATE `viabe-team-codex` clone on `codex/*` branches (never in the primary working tree). CC owns the TRUST-CORE umbrella VT-708, BLOCKED until post-launch-stability (not started before that gate). CC holds the review/merge lane for the Codex outer-ring PRs + custody of the sealed-set. HARD GUARDRAIL: Codex is FORBIDDEN from BOTH allocators (`scripts/vt_id_allocate.py` + `scripts/migration_id_allocate.py`) — CC allocates every VT-id and migration number on Codex's behalf up-front (mirrors the parallel-fan-out allocate-before-fan-out discipline, CL-424). Basis: Clau signal 20260722T121500Z.

## CL-2026-07-28-single-voice-manager — the Manager is ONE entity: single brain, single voice, single memory of what it has said; a silent contradiction is unacceptable (Fazal 2026-07-28, Standing / north-star)
Fazal ruled (verbatim): *"the manager is a single entity, single brain, single point of contact, he cannot say two different things or contradict himself… has to be intelligent, active, with presence of mind, with active context, self-aware, self-learning."* The Manager is "the YOU for the business" — Claude-grade presence of mind, executing business actions. **Engineering diagnosis (CC):** ~7 surfaces currently speak with partial context (signup machine, journey walker, journey turn-brain, enforce gate, consent direct-handlers, paced-flow beats, OAuth resume gates); the observed defects — two-people drafting, the "No changes, looks good"→consent-decline misfire, completion-boundary double replies, taxonomy-vs-website contradiction — are ONE disease (multiple mouths), not four bugs. **Program (staged):** S1 wire-truth compose context + deterministic kickoff + typed-twice guard + website-wins + stale-push guard (largely SHIPPED: VT-716/716b/717/699). **S2 (VT-718)** SINGLE EMISSION CHOKE — every owner-bound send from every surface flows through ONE gate (wire-history-aware dedup, contradiction block, continuity/tone check); no bypass path, enforced by a CI gate in the style of `gate-no-raw-railway-variables`. **S3 (VT-719)** ASSERTED-FACTS LEDGER — a durable store of facts/commitments the Manager has TOLD the owner, consulted at compose time; a change must be explicitly OWNED ("earlier I said X — that's now Y because…"), never silently flipped. **S4 (VT-720)** ROUTE UNIFICATION — today's gates become input classifiers feeding ONE composer; run-6 defects get fixed structurally rather than patch-by-patch. Ties to O8: see CL-2026-07-28-o8-living-knowledge — S3's ledger and O8 versioning are ONE system.

## CL-2026-07-28-o8-living-knowledge — the O8 corpus is the Manager's ACTIVE, LIVING knowledge, not a static library (Fazal 2026-07-28/29, Standing; binding on VT-710/711 review criteria)
Fazal ruled (verbatim): *"the RAG implementation we are doing in parallel is to increase the manager's active knowledge and that this knowledge will keep increasing, will change, will get affected basis on the experience the manager has, the response the tenant gives, the outcome the business gets, the diversions the VTR adds, and also basis on the current affairs, business news, etc."* **Binding consequences, to be designed in NOW (cheap at design-time, expensive later):** (1) the learning loop (VT-711) must treat **tenant responses, business outcomes, and VTR diversions as first-class knowledge-mutation SOURCES with provenance** — not merely as evaluation signal; (2) a **NEW ingestion class — current affairs / business news**: recurring, freshness-dated, decaying, with its own rights and retention posture (out of current WP scope; rostered separately); (3) **versioned changing knowledge is the single-voice tie-in** — when knowledge CHANGES, the Manager must OWN the change in conversation via the S3 asserted-facts ledger (CL-2026-07-28-single-voice-manager), never silently contradict last week's advice. **The S3 ledger and O8 card versioning are ONE system, designed together, not two.** Recorded into `.viabe/o8-knowledge-engine-design.md` §12 and relayed to Codex as review criteria.

## CL-2026-07-29-launch-with-rag — O8 UN-PARKED and launch-critical: we launch with the knowledge engine in place; the Manager owns its knowledge and learns from tenant one (Fazal 2026-07-29, Standing)
Fazal ruled: *"We will prioritise S2/S3/S4, we will launch with the RAG in place, thats our moat and we need our manager to own the knowledge and start learning from the beginning."* **Consequences:** (1) **O8 status PARKED → IN PROGRESS (launch-critical)** — it is no longer sequenced behind the trust floor; it ships with launch. (2) The single-voice program (VT-718/719/720) is **prioritised** — and is a hard dependency, not a parallel track: knowledge that changes requires the asserted-facts ledger so the Manager owns corrections rather than contradicting itself (CL-2026-07-28-o8-living-knowledge §12.3). (3) The learning loop must be **capturing from tenant one** — experience, tenant responses, business outcomes, VTR diversions — so the moat begins compounding at launch rather than after it. (4) Prod migrations 184/185 applied live by CC on Fazal's authorization. **Clau's binding caveat, recorded (Fazal may override):** LEARNING from day one and SERVING unvalidated curated cards from day one are separable. Capture is pure upside and carries no tenant risk; serving curated cards to tenants before the O11 baseline exists inverts "measured, not asserted" on the exact surface (business advice) where a wrong card is most expensive. Recommended shape: capture live from tenant one · curated corpus serves in shadow until the baseline lands, then graduates per §6 · tenant-specific learned knowledge (L1/L2, the tenant's own history) may serve immediately since it is the tenant's own ground truth, not an unvalidated external claim.

## CL-2026-07-29-manager-owns-memory — the MANAGER's memory is the primary knowledge estate; specialist memory is thin and task-specific; cards must be flippable in/out of any memory scope (Fazal 2026-07-29, Standing)
Fazal ruled: *"build it such that we can flip the cards in/out of the managers and other agents global memory or tenant specific memory based on situation… Remember the Manager's memory is the important element, rest other agents memory is only limited and specific to their specialised tasks, mostly to cater to the agent specific customisations."* **(A) Memory-ownership model (resolves the §5.7 tension):** the MANAGER is the knowledge holder — the global corpus and the tenant's learned history are the Manager's memory estate. Specialist agents do NOT hold parallel knowledge estates; each carries a THIN, task-specific memory serving agent-specific customisation (e.g. SR's copy conventions, cohort heuristics, what phrasing worked for THIS tenant), not general business knowledge. This coexists with Codex's R3 retrieval-depth rule — that rule is about CONTEXT BUDGET per turn (don't flood the Manager with deep corpora on every turn), not about where knowledge LIVES. Ownership = Manager; per-turn depth = scoped retrieval. **(B) Card assignment must be FLIPPABLE at runtime:** every card carries an assignment — Manager-global · Manager-tenant-specific · specialist-scoped (per agent) · disabled — and it must be changeable by operation without a rebuild or migration, per card and per scope, with the change recorded as a lifecycle event (append-only, attributable, reversible). Situational reassignment is a first-class operation, not a schema change. **(C) Launch posture (supersedes Clau's shadow-first recommendation — Fazal heard the caveat and ruled):** the 118 curated cards are INCLUDED from launch, not shadowed. Rationale: they are Codex-curated by information type; the Manager's behaviour under real conversation is the evidence Fazal wants, and the attribution substrate (`decision_evidence_links`) makes that observation instrumented rather than anecdotal — every card that influences a decision is logged, so a harmful card is traceable and flippable-out immediately. The O11 baseline still lands and still governs formal graduation; inclusion-at-launch is an observation posture, not a claim that the cards are validated.

## CL-2026-07-29-manager-is-coo — CANONICAL role definition: the Manager is the COO of the tenant's business; specialists are expert doers with thin customisation memory (Fazal 2026-07-29, Standing — supersedes scattered role text everywhere)
Fazal's definition, ratified verbatim as the canonical role contract. **THE MANAGER IS THE COO — the one running the business.** It must: **(a)** have full knowledge of how to run a business; **(b)** have a good understanding of THIS tenant's business; **(c)** be capable of defining a roadmap AND a next-7-day plan for the tenant's business, **revising that plan every day** factoring the previous results and the next 7 days; **(d)** identify the right action/tool/agent and feed them the right **directive, input and objective**; **(e)** evaluate the action/tool/agent's IMPLEMENTATION PLAN — validate it, correct it, auto-approve it, or share it with the owner for approval; **(f)** evaluate the OUTCOME against the expected outcome and objective; **(g)** self-learn from that outcome. **THE SPECIALIST AGENT** must have complete knowledge and capability for its specialised task; it receives directive + inputs + objective from the Manager and must produce its own IMPLEMENTATION PLAN; it holds a **thin memory of its own** used to track the diversions, changes and customisations THIS tenant needs in that specialised action — **conveyed to it as cards by the VTR or by the Manager.**
**Three Clau clarifications recorded with the ruling (Fazal to correct if any misreads him):** **(1) Two approval layers must never blur.** (e)'s "auto-approve" is PLAN approval — the Manager judging a specialist's implementation plan, and auto-approval there is earned per capability (C4 graduation). It is NOT effect approval: customer sends, money and consent still pass the deterministic gates and Pillar-7 owner approval, always, regardless of how confident the Manager is about the plan. A Manager-approved plan whose execution touches an effect still stops at the gate. **(2) (c) is a scope increase over today's §7A** — the built loop plans monthly/daily; a ROLLING 7-DAY plan REVISED DAILY against prior results is net-new: rostered as **VT-721**. **(3) Specialist memory now has a WRITE PATH** — VTR and Manager write customisation cards into it. That path is governed like any other knowledge write: provenance recorded (who wrote it, VTR vs Manager, and why), lifecycle event on every change, tenant-scoped + RLS + DSR-purgeable, and it never becomes a general-knowledge estate (CL-2026-07-29-manager-owns-memory §13.1 stands).

## CL-2026-07-29-o8-engine-complete — the O8 knowledge engine is BUILT (inert): #543 + #545 merged; remaining gates are activation gates, not build work (CC merge under the O8 program grant, 2026-07-29)
CC merged the Codex outer-ring stack in order — **#543** (VT-710: scanned ingestion + retrieval corpus, 118-card conversion with rights manifest) then **#545** (VT-711: inert learning loop, admission, rollout controls, specialist memory + card assignment; migration 186) — head `1de166c9` → dev `170b2b99`. **CC verification pass (beyond Clau's branch audit):** fresh-DB proof green — all 187 migrations apply (186 + CC's 187 coexist), DSR hard-delete canary passes (the append-only trigger's transaction-local `app.dsr_purge` GUC exception works; tenant roles remain RLS-blocked from deletes regardless), knowledge suite + asserted-facts suite green together (327 passed). Inert boundary held: no non-knowledge module imports the new O8 modules (only dsr_purge's purge inventory), rollout default `off`, and `off` mode structurally cannot carry dormant activation authority. No-effect-authority: no send-choke or effect gate reads cards. Numbers clean: 186 only (182/183/186 = the allocated Codex set; 187 = CC's VT-719, no collision — Codex rebased onto it). CI note: the single red on both PRs was `test_in_memory_retrieval_latency_is_bounded` (a 100ms wall-clock bound; 410ms on a loaded shared runner, green locally) — runner-noise class; flagged to Codex to loosen or mark advisory. **O8 is now BUILT. Remaining gates are not build work:** sealed set + O11 baseline (Clau) · rights position on the 96 unknown-rights cards (Fazal) · graduation thresholds (Fazal) · the activation flip (Fazal, per CL-2026-07-29-manager-owns-memory launch posture).

## CL-2026-07-29-card-rights-licensed-sources — unknown-rights cards are REPLACED with licensed-source distillations, not shipped under a fair-use position (Fazal 2026-07-29, Standing)
Fazal ruled on the 96 `unknown`-rights cards in the 118-card curated corpus (public accessibility was correctly NOT treated as a licence by Codex; those cards are `rights_blocked` from embedding and therefore cannot serve). **Ruling: REPLACE, don't risk.** Keep the cards with clean rights (3 `permission_granted` RKECOM-authored + the corroborated remainder); progressively replace `unknown`-rights cards with distillations from clearly-licensed sources — World Bank / NBER / J-PAL / IPA / government / RBI-SIDBI and equivalents. Rationale: zero legal exposure for a launching company; the licensed evidence base is stronger material anyway (primary studies beat practitioner retellings); and O11 will show which cards actually earn retrieval, so replacing weak-provenance cards costs less than defending them. **Consequence:** the launch corpus is SMALLER than 118 — that is accepted. Coverage grows as licensed distillations land. The 5 `live_link_only` cards remain reference-only (they already never claim the local synthesis is the source). Rostered: **VT-723** (licensed-source distillation programme). No fair-use position is taken or recorded anywhere.

## CL-2026-07-29-graduation-thresholds-clau-proposes — Clau drafts the O8 graduation numbers, Fazal ratifies (Fazal 2026-07-29, process)
Fazal's process ruling on the §6 graduation gate (minimum sample sizes, non-inferiority margins on safety-critical slices, confidence thresholds — which Codex deliberately left unset rather than guess): **Clau proposes concrete numbers grounded in the journey-pack's observed statistics; Fazal ratifies or adjusts.** He keeps the decision without performing the statistics. Binding sequencing: the proposal is only meaningful once the O11 sealed set + frozen baseline produce real distributions, so the draft lands WITH the baseline, not before. Until ratified, NO corpus version may be marked graduated/validated on measured grounds — inclusion-at-launch (CL-2026-07-29-manager-owns-memory §13.3) remains an observation posture, not a validation claim.

## CL-2026-07-29b-knowledge-not-source — SUPERSEDES CL-2026-07-29-card-rights-licensed-sources: inclusion turns on the card's ACCURACY and VALUE and on ORIGINALITY OF EXPRESSION — not on the source's licence (Fazal 2026-07-29, Standing)
Fazal corrected Clau's over-cautious position: *"how would license matter for the 96 unlicensed cards? We are only using their information as a knowledge, and our criteria of inclusion and exclusion must be the accuracy and value of the knowledge provided by the card and not its source."* **He is right, and the earlier ruling is superseded.** Copyright protects EXPRESSION, not facts or ideas (the idea/expression dichotomy). A card that re-expresses an extracted finding in our own words is knowledge, not a reproduction — the 118 cards are Codex-authored structure and sentences (situation / decision pressure / risk / recommended action), with none of the source's expression surviving. **The gate is therefore RESET:** (1) INCLUSION = accuracy + value + measured impact (§6), never source licence; (2) `usage_rights: unknown` no longer blocks embedding or serving — the 96 cards are eligible; (3) provenance/source-class remains, but for its legitimate purpose — **AUTHORITY WEIGHTING** in retrieval ranking and conflict resolution (T1–T4), not eligibility. **Narrow cases where source still binds, retained deliberately:** (a) **expression-originality check** — a card containing verbatim or near-verbatim source text is a reproduction and is rejected/rewritten regardless of licence (this replaces the rights gate as the real check); (b) **compilation/database rights** — wholesale extraction of a substantial portion of ONE source/dataset can attach rights even where individual facts don't; flag if a single source dominates; (c) **contractual ToS** prohibiting extraction — binds by contract, not copyright; (d) paywalled material obtained by circumventing access — excluded; (e) **raw archived source pages remain local-only and never retrieval-eligible** — those ARE reproductions (the existing rule, now correctly justified); (f) the 5 `live_link_only` cards keep their honesty convention (never claim the local synthesis is the source original). **VT-723 is RE-SCOPED** from "replace unknown-rights cards" to "grow coverage from high-authority licensed sources where O11 shows weak slices" — additive, not remedial. Clau's note for the record: this is a legal framework, not legal advice; if a specific publisher's ToS or a dominant-source concentration surfaces, that is a counsel question, not an engineering one.

## CL-2026-08-03-seed-then-full-ingestion — Standing (Fazal)

**Decision.** O8 fills the registry in two stages: a small seed corpus to prove the serving path
end-to-end (**VT-726**), then the **FULL 118-file ingestion (VT-727), which must not be missed**.
Fazal, verbatim: *"Seed a small corpus, prove the pipe, and once proved ensure all 118 files are
ingested. The full ingestion must not be missed."*

**Trigger.** CC's 2026-08-03 bounce of VT-725: every O8 table held ZERO rows. Clau had asserted
"118 eligible cards" — those are FILES, never ingested. Decisive blocker: `knowledge_cards` has
no `domain` column while `CardRetrievalEngine` filters on `card.domain` first.

**Binding sub-rulings (Clau, within the grant):**
- `domain` is a **first-class NOT NULL column** on `knowledge_cards` (migration 189), indexed
  `(domain, status)` — never a per-retrieval 3-way join, never derived.
- **Seed cards MUST enter through the VT-710 pipeline. A hand-authored direct INSERT is
  forbidden** — it would rebuild the authored-playbook mechanism Track-D retired and R1 killed,
  and would prove nothing about the pipe.
- VT-727 does not close because VT-726 proved the pipe. Per-file verdict for all 118; a silent
  drop is a row failure.

**Lesson recorded (Clau).** Third premise error in this program — runtime state asserted from
documents instead of verified against the database. CC verified; Clau did not. Bouncing a row
with evidence is correct behaviour and is to continue.

## CL-2026-08-03-green-on-fallback-is-not-green — Standing (Clau, from a CC finding)

**Rule.** A green result is not evidence until you know **which code path produced it**. A pass on
a fallback, a stub, a cached value, or a skipped branch reads identically to a pass on the
intended path. Before banking a canary or a scenario class, confirm from logs/traces that the code
you changed is the code that ran.

**Trigger.** VT-720, 2026-08-03: `_invoke_llm` read `resp.content[0].text`, which is a
`ThinkingBlock` when extended thinking is on — so every converted route silently fell back, and
casebook classes 1-2 went **3/3 on the fallback line**. CC caught it only by reading logs instead
of banking the green. **Third instance of this family after VT-662.**

**Companion to** `CL-2026-08-03-seed-then-full-ingestion`'s lesson (Clau asserting runtime state
from documents). Same disease from opposite ends: trusting the artifact instead of verifying the
mechanism. Both landed in one week.

## CL-2026-08-03-degradation-before-measurement — Standing (Clau)

**Rule.** Do not run a measurement pack into a degraded environment. A pack measured on unstable
infrastructure manufactures phantom failure classes and costs the window twice — once producing
them, once chasing them. Settle the environment, then measure.

**Trigger.** CC's 2026-08-03 refusal to run the promotion full-pack ×3 into a dev showing 90s
timeouts and `route='none'` on still-running turns (VT-728). Third occurrence of the family after
VT-719 deploy-contamination and VT-722 held-pushes.

**Corollary.** Dev orphaned-workflow accumulation is the dev-side face of **VT-634** (prod
failed/orphaned workflow handling, launch-blocker). Findings from a dev degradation of this class
are input to VT-634, not throwaway fixes.

## CL-2026-08-03-docs-push-is-a-deploy — Standing (CC finding; Clau error)

**Fact.** Railway's native auto-deploy fires on **EVERY push to `dev`, including docs-only pushes**
that the VT-245 CI trigger-diet deliberately skips. **A skipped CI run does not skip the deploy.**
A `docs(sprint): …` push restarts the orchestrator.

**Consequence.** A push during a measurement run splits one measurement across two services and
manufactures phantom TIMEOUT / `terminal=running` / unobserved-route results. This is the actual
cause of the VT-720 "dev degradation" (VT-729) and almost certainly of the VT-719 "contamination"
that cost 7 of 11 blocks.

**Origin of the error: Clau's own Rule 3** ("push docs on receipt", to stop Codex reading a stale
tree). Optimising for Codex's freshness was paid for in unreadable measurements.

**Rule 3 amended:** push docs on receipt EXCEPT while a measurement run is in flight.

**Structural guard (VT-729, preferred over the discipline note):** `run_critical_x3.py` reads
`dbos.application_versions` before and after each scenario; a change labels the run
**CONTAMINATED**, never BLOCKED. CC's reasoning is the point — *"a discipline note would not have
saved me; I was holding code pushes. It was the docs push I did not think of as a deploy."*
Prefer a guard that does not depend on remembering.

**Known follow-on (not fixed):** harness teardown cascades `pipeline_runs` away, so after-the-fact
latency forensics on a finished run are impossible; this diagnosis had to be reconstructed from
deploy timestamps.

## CL-2026-08-04-report-bundles-are-append-only — Standing (CC finding; Clau error)

**Fact.** `run_critical_x3 --json-report` is **APPEND-ONLY and never truncates.** A bundle reused
across attempts silently accumulates entries from BOTH runs.

**What it caused.** Clau read 186 entries ÷ 3 = "62 of 79 scenarios, ~78% done" and reported that
to Fazal. The bundle held two attempts: 129 distinct runs across 43 scenarios, 13 of them recorded
twice. **Real progress was ~44 of 79 (~55%).**

**The worse consequence, avoided.** The same polluted bundle feeds `transcript_judge.py`, which
would have scored 13 scenarios **twice** and inflated the aggregate. The measurement substrate
silently merged two different runs — a corrupted conclusion, not merely a wrong progress number.

**Rules.**
1. **A fresh measurement uses a FRESH report path.** Never reuse a bundle path across attempts.
2. **Derive progress from DISTINCT scenario names, never from entry count ÷ 3.**
3. Before trusting any aggregate, check the bundle for duplicate scenario entries.

**Class.** Third instance in one week of *"something we assumed was inert, wasn't"* — after
`CL-2026-08-03-docs-push-is-a-deploy` (a docs push redeploys) and the DBOS `compute_app_version`
finding (a restart re-recovers PENDING rows). Clau's own error here is the same shape as
`CL-2026-08-03-green-on-fallback-is-not-green`: an artifact read without asking what produced it.

**Resolution (VT-729 follow-on, built by CC).** `--resume` reuses a prior scenario ONLY if all three
runs are recorded, all three clean, and all three carry the SAME `dbos.application_versions` value
the service is running now. Version mismatch ⇒ re-driven, reason printed. A resumed pack prints
`!! RESUMED RUN: N scenario(s) came from a PRIOR segment` — both guards are **refusals, not
annotations**, because an annotation is what a tired reader skips.

## CL-2026-08-06-pricing-structure-ratified — Standing (Fazal)

**Structure (ratified verbatim 2026-08-06; LEVEL still pending VT-733-C measured costs):**
1. **The Team Manager is FREE.** Revenue is per SPECIALIST.
2. **Flat price per specialist per month, everything included** (AI, WhatsApp, integrations). No
   metered billing surface — caps are internal guardrails, never invoices.
3. **Every agent's first month free, per agent, activation-timed** — permanent rule, so each newly
   launched specialist arrives as a free test-ride to every tenant. **UPI autopay mandate collected
   at trial activation** (₹1 auth); trial auto-converts unless cancelled.
Supersedes the ₹5000/agent figure (viabe-pricing-trial-model): structure stands, level is an open
number set from measured cost-per-tenant-month. No pricing input from estimates.

### ⚠️ COST-BASIS WARNING added 2026-08-10 — the free in-window assumption EXPIRES 1 Oct 2026

**The pricing LEVEL is still unset and waits on VT-733-C measured cost. Whatever that measurement
says, it is measured against a cost basis that changes in ~7 weeks.**

Meta India per-message rates, effective **1 July 2026** (verified by web search 2026-08-10, and the
kb citation was corrected from a wrong "1 January" date): **marketing ₹0.8631**, **utility ₹0.1150**,
**authentication ₹0.1150** — marketing is **7.5× utility**. Plus **18% GST** and any BSP fee.

**The expiry:** utility and service messages inside the 24h customer-service window are **free only
until 1 October 2026**, after which they become chargeable.

**Why this hits us specifically** — three live design decisions assume free in-window messaging:
- the **template-whitelist ruling** (Fazal 2026-07-18): owner template surface is minimal and
  *everything else rides the 24h session*. That "everything else" acquires a unit cost on 1 Oct.
- **VT-683** queued owner-comms, idle-paced inside the session — same.
- **VT-741 Tier A** (once/24h for engaged customers) was cheap precisely because a customer who
  replies opens a free session window.

**Action:** VT-733-C must model BOTH cost bases (pre- and post-1-Oct) or the price level will be set
against economics that expire before the first renewal. **Verify the exact scope against Meta's own
published pricing documentation before pricing** — the 2026-08-10 finding came from secondary
sources (BSP blogs), which are directionally reliable on rates and looser on scope.

### SUPERSESSION RECORDED (Fazal 2026-08-10: "record the supersession, and ensure no other decision conflicts with it")

This ruling **supersedes two earlier standing decisions on three specific points.** Recorded here
because CC read the contradiction in code and could not tell which side was current — which is the
correct failure mode, and the ledger's fault, not CC's.

**1. CL-433 (2026-06-09) is SUPERSEDED on the card/convert mechanics.** CL-433 ruled: *"NO card
captured during the trial; the owner **actively subscribes** at/after day 30 (an explicit action —
there is **NO auto-charge edge**)."* The 2026-08-06 model inverts both halves: a **UPI autopay
mandate IS collected at activation** (₹1 auth, i.e. during the free month), and the trial
**auto-converts unless cancelled**. Explicit-subscribe-at-day-30 is dead.
**What SURVIVES from CL-433, unchanged:** the 30-day/one-month free period · **NO refund ever**
(the whole refund subsystem stays deleted — do not resurrect VT-85/92/93) · `lapsed` as the
dormant, re-subscribable phase · the GSTIN gate riding the subscribe edge (CL-442 moved the primary
gate to signup regardless).
**Code consequences, unbuilt — these are real work, not bookkeeping:**
- The state machine has **no auto-convert edge**. `trial_expired → lapsed` is the only elapse path
  (`transitions.py`; migration 121 reshaped the phase CHECK around it). Auto-convert needs a new
  edge: mandate-charge succeeds → `paid_active`; charge fails → dunning, then `lapsed`.
- Razorpay `payment.captured` currently maps to the `subscribe` EVENT, which presumed an explicit
  owner action. Under auto-convert the capture arrives with no owner action at all.
- Per-agent trial timing: the free month is **per specialist, activation-timed**, so trial state
  can no longer be a single tenant-level phase — a tenant may hold one specialist in trial and
  another paid. The current phase model is tenant-scoped and cannot express that.
- `billing/trial_sweep.py:367` applies `trial_expired` today. Under this ruling that sweep must
  attempt the mandate charge first, not transition straight to `lapsed`.

**2. The 2026-06-25 activation pin is AMENDED, not overturned.** `onboarding_gate.py:9-11` records
Fazal's pin: *"the activation bar is journey-complete, NOT paid-active. The 1-month free trial is
DELIBERATELY UNRESTRICTED."* That still holds in its intent — **activation never requires a
PAYMENT, and the free month stays genuinely free.** What changes: activation now additionally
requires a **mandate on file** (₹1 auth, not a charge). So the bar is journey-complete **+ mandate**,
never paid-active. The `paid_active` conjunct stays removed exactly as pinned.
**The tension Fazal should see once, then it is settled:** requiring a mandate at activation IS
friction the original pin deliberately removed, and it will cost some activations. Fazal's
2026-08-06 ruling accepted that trade knowingly (auto-convert is worth more than frictionless
activation). Recorded so nobody re-opens it as a bug.

**3. Per-specialist pricing does not exist in code.** `config/plans.yaml` is **flat per-tenant
tiers** (founding ₹2,499 / standard ₹4,999 / pro ₹14,999, `offered_tiers: [standard]` fail-closed).
That is not "the level is unset" — it is a **different pricing model** from the ratified one, live
in config today. Rebuilding it per-specialist is unrostered work.

**Swept for further conflicts (2026-08-10) — none found beyond the above.** CL-442 (GSTIN hard-gate
at signup) is orthogonal. CL-426 (VTR decaying human-on-the-loop) is orthogonal. CL-2026-08-06-budget-aware-manager
is complementary — caps stay internal guardrails and never become invoices, which is consistent
with flat-per-specialist. The `docstring` at `api/razorpay_subscribe.py:6` still claims the
integration is "STUBBED; LIVE is NEEDS-FAZAL" while `:123` records the stub as replaced — a stale
status line inside one file, rostered for correction.

## CL-2026-08-11-judgment-is-commodity-execution-is-the-moat — Standing (measured)

**Measured, not argued.** 25 India-SMB judgment scenarios. Fazal answered cold; Codex answered blind
by paste. Scored on decision RULE, not option label, against a prediction pre-registered before the
comparator ran. Full working: `.viabe/calibration/RESULT-fazal-vs-codex.md`.

**Result: 22/25 directional agreement (88%). Only 3 genuine rule-level divergences.**

**Ruling: do NOT build a business-judgment corpus.** A generic frontier model already reaches Fazal's
call on 22 of 25 situations in our own domain — including the *computed* ones. Corpus authoring stays
cancelled permanently, not pending. Two independent reasons now: the 825 were AI-generated
(CL-2026-08-11-no-human-calibration-baseline-exists), AND the exercise they served is low-value.

**REPLICATED 2026-08-11 with a SECOND independent model (ChatGPT).** Same distribution — 16 same /
6 compatible / 3 different — but a different composition. **Q02 and Q05 diverge in BOTH arms, in the
same direction: those replicate and are signal.** The other divergences are single-arm variance.
**Codex and ChatGPT agree with EACH OTHER ~22/25**, so the residual is mostly model variance rather
than a stable human-vs-machine axis.

**The delta, restated after replication — this supersedes the arm-1 wording:**
> **Fazal commits to one clean position. The models hedge, split, or preserve optionality.**

Q02 (removes the low-end option and repositions premium; both models build tiers and keep a cheap
one) · Q05 (pays in full on the relationship; both models contractualise AND qualify a backup) ·
Q18 (hires now for direct contact; Codex defers to a threshold) · Q08 (borrows entirely personally;
ChatGPT splits to land exactly on the floor). Relationship-weighting is a *consequence*;
**decisiveness is the root.**

**PRODUCT IMPLICATION — the most useful output of the exercise.** An LLM Manager defaults to hedged
recommendations: two tiers, a contract plus a backup, a threshold to revisit. To an SMB owner needing
a decision today, *"here are two options"* is what they already get from everyone, free. **Hedging is
a product failure mode, not a safety feature.**
> **Design principle: the Manager must COMMIT to a recommendation. Where it hedges, it must say which
> door it would walk through and why.**
Applies to REASONING only. The deterministic effect gates stay exactly as unbendable as they are
(ARCHITECTURE §0.1.1) — commit in the advice, never in the rails.

Q02 (refuses to keep a low-end option; repositions premium) · Q05 (pays ₹1.8L on six years of
relationship rather than converting it to a contract + hedge) · Q18 (pays fixed cost now to own the
customer relationship) · Q07 partial (takes the 22% money on a computed bounded bet).
**Plausibly CORRECT for the domain** — in Indian SMB the relationship IS the supply chain and the
retention mechanism; a contractual optimiser is applying a US-enterprise prior. **Encode as 3–4
Manager operating principles, Fazal-reviewed. A card or two, never a knowledge base.**

**Consequence — where the moat actually is:** if the Manager's judgment is commodity, everything
defensible sits in (a) doing the work reliably and safely — V1-USABLE E1–E6 — and (b) the §12 loop
capturing what actually happened to real tenants. Anyone can copy business advice; nobody can copy
tenant outcomes.

**Clau's prediction FAILED and that is recorded deliberately.** I predicted divergence on Q01, Q05,
Q09, Q22, Q25 — **one clean hit in five.** My thesis was that a generic model would be weak at
computed trade-offs; **it was not** — it did the Q01 overdraft arbitrage I had singled out as Fazal's
"single most likely outperformance." My model of where frontier models are weak was out of date, and
the instrument existed to catch exactly that. **Method note:** I fixed numeric thresholds but never
specified strict-vs-directional agreement, which left post-hoc latitude a pre-registration exists to
remove. Resolved on reasoning (3 genuine divergences), not on the arithmetic.

## CL-2026-08-11-no-human-calibration-baseline-exists — Standing (Fazal disclosure; Clau retraction)

**Fazal, verbatim 2026-08-11:** *"The 225 + 100 of batch1 are not manually answered, they are
answers from a completely distant unaware AI who was asked to act as a Business expert and respond."*

**Ruling: the verified human-calibrated baseline is ZERO.** Every prior count is withdrawn — 825,
325, and 225. The fazal-kb calibration corpus is an unrelated model role-playing a business expert.
Classified **T4 (unsourced third-party assertion)**: **not retrieval-eligible, not DPO-eligible,
never asserted to an owner.**

**Why T4 and not "validate it up":** as retrieval cards these are the commodity layer — a frontier
model already holds this knowledge, which is very likely the mechanical explanation for the O11
treatment NULL. As DPO preference pairs they would distil a weaker, unaudited model into ours.
Neither is a moat; both are risk. **You cannot validate another model's opinions into proprietary
judgment.**

**What survives:** the SCENARIOS and OPTION SETS are a legitimate question bank. Only the
SELECTIONS and RATIONALES are void.

**Clau's error, recorded because the discipline is the point.** Fazal relayed these in chat; Clau
recorded them as Fazal-authored, never asked who wrote them, and then built a 61KB artifact stamped
*"O8 Tier 1 — Verified Human Executive Judgment, Source: Fazal (CEO)"* across all 225 — while in the
same conversation criticising the kb creator for exactly this mislabelling. **Relayed-by is not
authored-by.** Provenance is a question you ask, not a property you infer from who sent the file.

**Standing rules this produces:**
1. **Every card records WHO AUTHORED IT, asked explicitly — never inferred from who transmitted it.**
2. **A size check is a provenance check.** A file claiming N records that is too small to hold N
   records is metadata. Check bytes before citing counts. (Caught the 720-byte "225 answers" file
   and the 1,760-byte "825 rules" file; missed on both until too late.)
3. **AI-authored content may never enter above T4**, and T4 is not retrieval-eligible.
   **AMENDED by CL-2026-08-13-judgment-vs-citation (Fazal) — see that entry.** The T4 ceiling binds
   AI-authored JUDGMENT. AI-DISTILLED CITATIONS of primary sources carry the source's class, with
   `authority=seed` recording AI authorship, IFF verifiable against the cited source. Unverifiable
   collapses back to T4.
4. **The moat requires a human or an outcome.** Only Fazal's own judgment, or a measured tenant
   result, can ever be Tier 1. Nothing generated can be promoted into it.

## CL-2026-08-13-judgment-vs-citation — Standing (Fazal: "Go ahead with your recommendation")

**Resolves the literal collision between CL-2026-08-11 rule 3 ("nothing AI-authored above T4") and
CL-2026-07-26 (source-class describes the SOURCE; authorship lives in `authority`).** Raised by CC
during the PR #553 review; both rulings survive because they govern different objects.

**The rule:**
> **AI-authored JUDGMENT — opinions, recommendations, distilled "lessons", anything whose truth
> rests on the author's reasoning — enters at T4, ceiling, permanently.**
> **AI-DISTILLED CITATIONS — restatements of a primary source's fact — carry the SOURCE'S class
> (t1/t1v/t2/t3), with `authority=seed` recording the AI authorship, ON THE CONDITION that the
> citation is VERIFIABLE against the cited source: resolvable source, binding hash, reproducible
> claim. A citation that cannot be verified against its source is not a citation — it is the AI's
> assertion, and it collapses to T4.**

**The boundary condition is enforcement, not paperwork.** CC's #553 blocking findings (source hash
binding zero of 33 cards; all archive files unreachable) are exactly the verifiability condition
failing — which is why #553 cannot land until they are fixed, and why fixing them is what makes
this ruling honest rather than a relabeling. **A distillation is never evidence of itself.**

**Test heuristic for future reviews:** ask "if the cited source vanished, would the claim still
stand?" If yes (it rests on the author's reasoning) → judgment → T4. If no (it falls with the
source) → citation → source's class, if verifiable.

**Consequence for the O8 program:** with this corpus reclassified, the honest statement is that
**Viabe holds no proprietary business-judgment corpus today.** The path to one is (a) Fazal
answering scenarios himself — 25 of his are worth more than 325 of a model's — and (b) the §12
living loop capturing real tenant outcomes. Both are unstarted.

## CL-2026-08-10-vetoes-compose-permits-do-not — Standing (CC-originated, Clau-recorded)

**Origin:** Clau audited VT-740 and found two frequency mechanisms on the same send path — VT-740's
per-recipient suppression and `customer_send.py`'s pre-existing `RECONTACT_SUPPRESSION_DAYS` /
`MAX_AGENT_CONTACTS_PER_90D`. Clau asked CC to pick one or declare an explicit precedence rule
("most restrictive wins"). **CC's answer was better than the question** and is recorded as the
standing principle.

**The principle:** *gates that can only VETO compose without any precedence rule. The moment a gate
can PERMIT, ordering becomes semantic and a tie-break rule is required.*

Two conjunctive vetoes yield most-restrictive-wins **by construction** — the outcome is identical
whichever runs first, so call order can never drift into being the decider. A written
"most-restrictive-wins" rule (what Clau asked for) is weaker: it is a convention a future edit can
violate silently. The structural property cannot be violated without changing the gate's *kind*.

**The guard that matters:** the property stops holding the instant either layer gains a branch that
PERMITS a send. So it is **pinned by test**, not by comment. Any gate that acquires a permit branch
must simultaneously acquire an explicit precedence rule.

**Why both mechanisms correctly survive here** (they answer different questions on different tables):
`RECONTACT_SUPPRESSION_DAYS`/`MAX_AGENT_CONTACTS_PER_90D` read `agent_customer_contacts` and bound
"how often may an AGENT cold-contact this customer" — narrower, stricter, agent path only. VT-740
reads the send ledger and asks "has this customer been delivered ANY message recently" — every path,
campaign fan-out included.

**Applies generally** to this codebase's stacked correctness gates (consent · opt-out · onboarded ·
`ownership_verified` · Pillar-7 approval · frequency · aggregate cap). They are veto-only by design,
which is why the stack has never needed an ordering spec — and why introducing a permitting gate
would be an architectural change, not a feature. Cf. ARCHITECTURE §0.1.1 (PLAN approval vs EFFECT
approval) and CL-2026-06-28-cc-full-autonomy (deterministic gates remain the sole effect authority).

## CL-2026-08-10-welcome-is-en-hi-only — Standing (Fazal)

**Fazal, verbatim 2026-08-10:** *"I agree with CC we must only use the En / Hi as part of the
Welcome message."*

**Ruling:** the welcome template sends in **English or Devanagari Hindi ONLY**. The approved
Hinglish welcome variant (`team_welcome4` hing, `HX7097590ccf0e901d893f78d9a9224e92`) stays
**registered but deliberately unused**. Signup continues to offer `en` / `hi` only
(`_LANGUAGES = {"en","hi"}` — correct as written, not a bug).

**Why this needed a ruling at all:** CC found the hing welcome SID is **unreachable by any code
path** — `template_register()` is never called by the welcome path, and signup rejects `hinglish`
outright. Clau had dispatched a "flip the hinglish register" task built on the premise that the
EN fallback was a stale gate; CC checked the premise, found `wakeup.py::wakeup_language` already
implements the exact guarded resolution, and changed only the stale comment. **The premise was
Clau's and it was wrong.** Fazal then ruled the behaviour is correct as-is.

**Precise scope of this ruling — it is about the WELCOME, not about Hinglish:**
- **`team_wakeup2` keeps its hing variant and it IS reachable.** `resolve_owner_locale` reads
  `COALESCE(preferred_language, language_preference, 'en')`, and `record_observed_language` writes
  `language_preference` from per-turn inference for any value in `SUPPORTED_OWNER_LANGS` — which
  includes `hinglish`. So a tenant who signs up `en` and then converses in Roman-script Hindi
  **becomes** hinglish, and their wake-up correctly resolves to `hing`. That path stays.
- **Free-form conversational mirroring is untouched.** D2 is unchanged: live-turn mirroring is
  never overridden, and a hinglish owner gets the hi-Latn register on free-form agent-initiated
  copy. This ruling constrains ONE template, not the product's language behaviour.
- **D1's binding half stands:** never Devanagari for a hinglish-preference tenant.

**Standing consequence:** do not "fix" the unreachable hing welcome SID. It is unreachable by
decision. Anyone auditing template coverage will find it and should find this entry.

## CL-2026-08-06-budget-aware-manager — Standing (Fazal; architecture-shaping)

**Fazal, verbatim:** "any consumption that is being done is triggered by the Manager, and never by
the owner, so the capping has to be there but the Manager needs be trained about it."

**Principle:** the tenant's budget is an INPUT to the Manager's reasoning, not a wall it crashes
into. The owner must never be punished (silent degrade, unexplained dumbness) for spending decisions
the MANAGER made. A real COO manages within a budget; ours must too:
- The Manager KNOWS its remaining tenant budget (soft/hard distance) as context.
- Approaching the cap, it ECONOMIZES deliberately — prioritizes high-value actions, defers
  low-value ones, prefers cheaper tiers where quality allows — and can say so to the VTR.
- The hard cap remains the deterministic backstop (mig 173), but reaching it silently is a
  MANAGER PLANNING FAILURE, not normal operation.
- Budget state feeds the rolling 7-day plan (VT-721) — plan revisions factor spend like any other
  resource constraint.
**Sequencing:** VT-733 records + surfaces; the budget-awareness context block + planning
integration is post-promotion design (rides with VT-730-era Manager work). Not in the current chain.

## CL-2026-08-06-pilot-proof-gate — Standing (Fazal)

**The post-launch success bar is FALSIFIABLE and recorded** (launch-tracker "Pilot Proof Gate"):
10–20 curated launch-persona tenants, 1–2 verticals, ≥8 weeks. P1 ≥60% week-8 active · P2 ≥40%
pay willingly at trial end · P3 ≥50% realise ≥3× product cost in ATTRIBUTED gross profit ·
P4 zero serious consent/money/contact violations · P5 onboarding repeatable without founder ·
P6 autonomy grants increase over time. **No specialist beyond the revenue cluster ships until the
gate reads green** (Fazal may override).

**Origin honestly recorded:** an independent non-Claude assessment (2026-08-06) whose probability
view Fazal found demotivating. Clau's reconciliation: its "corrections" restate the existing
strategy (wedge-first, curated personas, Meta analysis, attribution-centric); its base rates are
normal startup reality; its two genuine pressure points — pay-conversion risk and the
service-business trap — are adopted as P2/P3 and P5. Its "Revenue Conversion & Retention Agent"
= the roster's S1/S2/S3 cluster merged, adopted as SR's post-proof evolution path, not a new
build. **Attributed-gross-profit-per-tenant is now first-class in VT-733-C, attribution erring
UNDER, never over.**

## CL-2026-08-06-repeated-request-is-never-approval — Standing (Fazal, verbatim "Unambiguous no, as standing policy")

**Policy.** A repeated request is NEVER approval. An approval may be resolved ONLY by an inbound
message that satisfies BOTH:
1. **Ordering:** `created_at` strictly AFTER the approval was armed AND presented to the owner
   (deterministic check — a stale sid can never satisfy a future approval);
2. **Content:** an AFFIRMATION of the presented plan (deterministic-first classifier per the
   authoritative-decision pattern) — a re-ask, however enthusiastic, means "you are being slow,"
   never "I agree to specifics I have not seen."
If this costs an impatient owner one extra confirmation tap, that is the correct price on a
money path.

**Trigger:** VT-734 (2026-08-06) — an approval resolved by the owner's second ASK sent 72s BEFORE
the approval existed; Manager then claimed a send that never happened (campaigns=sent,
campaign_messages=0). Dev-safe (send-guard); on prod = 19 unapproved sends + a false claim.
O2 flipped MET → MET–INCIDENT OPEN until re-proven.

**Scope notes:** same disease as VT-730's ask/arm race — unchecked temporal ordering at an
ask/answer seam; fix as an ORDERING INVARIANT, not a path patch. Second root cause REQUIRED:
why the emission choke + money-truth binding admitted "sent to 19 customers" against 0 DB rows —
that claim should have been structurally unemittable twice over.

## CL-2026-08-06-vt734-closed-wedge-deferred — Milestone + two lessons

**VT-734 DEV-PROVEN** (mig 190; ordering invariant at the resolution choke + repeat-refusal ahead
of all classifiers; breach repro ×3 → pending/proposed/0-sent, was approved/sent/19). O2 re-close
rides the pack. **Wedge deferred BY FAZAL** ("defer", 2026-08-06) into the pack per CC's written
case: no longer reproduces on demand post-VT-734 (0/2; bulk 1/3→2/3); pack = the bigger sample;
call-out in the pack report either way, absence recorded not assumed.

**Lesson 1 (CC retraction):** the "fabricated send claim" was CC's own broken verification query
(joined a column the send path deliberately never writes — the wrapper's docstring says so). The
Manager told the truth; 19 sends were real (dev-guard mocked). Incident = pure approval-bypass.
Corollary: Clau demanded a root cause for a choke gap that did not exist — verify the defect
exists before requiring its investigation.

**Lesson 2 (standing):** "keep the tenant for evidence" does not survive the hourly harness
reaper. **Transcribe evidence into the row IMMEDIATELY at capture** — the row is durable, the
tenant is not.

## CL-2026-08-06-model-tier-policy — Standing (Fazal-ratified)

**The rule:** pay for latency exactly where a waiting human feels it — nobody waiting → FLEX (½×);
a person waiting → STANDARD (1×); a person waiting at a decisive moment → FAST (2×, allow-listed:
approval resolution [a VT-734 race-window SAFETY spend] · opt-out/STOP · future live-customer
first response). Degradation moves toward Standard from BOTH directions (Flex→Standard on
capacity; Fast→Standard on budget-exhaust + VTR flag — never across). Judge SCORING never Flex.
Deterministic by call-class from `.viabe/model-tier-policy.md` (the ratified source; changes are
Fazal-visible diffs). Measurement window open: spend-by-tier on the VT-733 console; Fazal
revisits after days of measured mix. Implementation = VT-735.

**Allocator note:** migrations 194 (VT-727 corpus load) + 195 (embeddings, only-if-split) issued
to Codex 2026-08-06; 191–193 = CC's unpushed lane work. Codex's refusal to assume 191 prevented
a real collision — the CL-424 discipline observed working.

## CL-2026-08-06-migration-191-retired — Housekeeping (binding)

**Migration number 191 is RETIRED — never to be used.** Consumed by an unattributed allocator
invocation (no file exists in the working tree, origin/dev, or any codex branch — verified by
Clau 2026-08-06); most plausibly a timed-out invocation on the slow shared FUSE mount that took
the number and lost the output. Joins 54/109/139 as documented gaps. Current accounting:
190=VT-734 · **191=RETIRED** · 192=VT-733A · 193=VT-733B · 194=VT-727 · 195=VT-727-reserved
(unused, documented) · counter=196.

**Guard (CC lane, next batch):** the allocator gains a one-line append-only JOURNAL
(`.viabe/sprint/.allocation-journal`: timestamp · number · claimed-by · purpose) written in the
same flock as the counter bump — so the NEXT abandoned allocation is attributable in one read
instead of an investigation. CC's re-allocate-don't-race conduct on discovering 192≠191 is the
reference behaviour and is why this surfaced as a gap, not a collision.
