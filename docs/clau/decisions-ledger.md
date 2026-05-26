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

---

## Notes on this reconciliation pass

**6 supersessions marked:** CL-20, CL-21 (privacy cluster → CL-390); CL-32 (→ CL-175); CL-33 (→ CL-35/CL-36); CL-57 (→ CL-324); CL-177 (→ CampaignPlan v1.0 via CL-260). Plus secondary supersession chains marked for the snapshot sequence: CL-307→CL-309→CL-317→CL-325→CL-375→CL-391→CL-394→CL-407.

**5 additions:** CL-260 (CampaignPlan v1.0 by-effect), CL-330 (owner_inputs structured-intent — THE current critical-path decision), CL-389 (privacy notice system-level framing), CL-407 (latest anchor). Discipline rules #12 (CL-322) and #13 (CL-324) re-tagged with the rule numbers in their lines so they're explicit as rule entries.

**1 dedupe:** CL-385 and CL-386 collapsed onto CL-386 line (kept the formal Fazal-approved entry).

**1 unverifiable reference:** Clau flagged UUID `366387c2-cc5a-81f1` as the CampaignPlan v1.0 / VT-37 page. Grep against `docs/clau/entries/*.md` AND `.viabe/sprint/*.md` returns zero matches. Likely either a UUID transcription glitch in Clau's note, OR a Notion sprint-board page that's no longer in the live data source. CL-260 is the strongest evidence available for the v1.0 decision so it's the citation used.

**4 discipline rules still TODO** at `docs/clau/discipline-rules.md`: #6, #7, #10, #11 — no CL entries define them. Possibly renumbered duplicates of early rules. Awaiting Clau dump or confirmation.

**Per Rule #14 itself (CL-386):** every line in this ledger has been verified against its source `entries/CL-<N>.md` file. The reconciliation is not from memory.
