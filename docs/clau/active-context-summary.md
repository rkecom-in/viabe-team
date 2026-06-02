# Active Context Summary — Cowork's working digest

**Purpose:** ONE file Cowork reads to know what's currently in force. Built from `docs/clau/entries/CL-*.md` (369 session-log entries) + `.viabe/sprint/*.md` (167 task briefs). Active-only — anything explicitly deferred, superseded, or completed-and-archived is dropped to the bottom or removed entirely. The substrate for `scripts/check_brief_against_ledger.py` and the mandatory pre-brief-ready check (Rule #16).

**Owner: Cowork. Discipline: update this file on every important decision, every brief-ready dispatch that touches a new domain, every merge that closes a phase, every Fazal directive that locks something. If something material happened and this file didn't get updated, the rule is broken.**

**Format:** topic-grouped rows. Each row: `[CL-N] (date) — one-sentence claim, why-still-in-force OR what-replaced-it.` Tags inline in the topic header so the grep script can surface relevant blocks.

**Coverage gap (transparent):** This file is currently populated only from `docs/clau/decisions-ledger.md` (Standing-decisions derivative). The full sweep of all 369 CL entries + 167 sprint briefs is pending — see `## Sweep TODO` at bottom. Until that sweep lands, expect to find load-bearing context in raw `docs/clau/entries/CL-<N>.md` files that this summary doesn't cite. When you discover one, add it here.

---

## Active Standing decisions (load-bearing, do not re-litigate)

| CL/DR | Date | Domain tags | One-line | Status |
|---|---|---|---|---|
| CL-2 | 2026-05-12 | deployment, infra, repos | Sibling repos + shared accounts + separate projects per account | Standing |
| CL-4 | 2026-05-12 | launch, milestones, dates | Reports Jun 15; Team soft Jul 15 (10 design partners); Team full launch | Standing |
| CL-5 | 2026-05-12 | whatsapp, templates, meta, tier-a, tier-b | Tier-A 5 launch-blocking + Tier-B 17 concierge-until-approved | Standing |
| CL-11 | 2026-05-12 | whatsapp, templates, meta | Meta template count grew 8→22 across batches; full list in VT-108 | Standing |
| CL-14 | 2026-05-12 | memory, three-layer, instructions, session-log, resurrection | Three-layer memory: project instructions (L1) + session log (L2) + resurrection (L3) | Standing |
| CL-16 | 2026-05-12 | fazal, communication, preferences, style | Fazal's communication preferences are STANDING every session | Standing |
| CL-19 | 2026-05-12 | step-records, envelopes, typed | Step records use typed envelopes (declared fields per step) not full-row snapshots | Standing |
| CL-22 | 2026-05-12 | ops-console, phase-1, launch-blocking | Ops UI is Phase 1 launch-blocking; new parent VT-OpsConsole | Standing |
| CL-23 | 2026-05-12 | mermaid, diagrams, styling | Mermaid: stroke-width:2px, color:#000, lighter fills with dark text | Standing |
| CL-24 | 2026-05-12 | orchestrator, agent, opus, brain, specialists | Orchestrator-as-agent locked: Opus 4.7 brain + own memory + spawns specialists | Standing |
| CL-25 | 2026-05-12 | filtering, pre-filter, two-stage | Two-stage event filtering: deterministic pre-filter then orchestrator brain | Standing |
| CL-26 | 2026-05-12 | memory, l0, workspace, orchestrator | L0 memory tier added: workspace-level operational memory | Standing |
| CL-28 | 2026-05-12 | k-anonymity, privacy, reports | K-anonymity reverted to k=10 per concept doc Section 10 | Standing |
| CL-29 | 2026-05-12 | orchestrator, triggers, event-driven | Orchestrator triggering: event-driven plugin-mediated, NOT continuous-loop observer | Standing |
| CL-35 | 2026-05-12 | durability, checkpointer, dbos | Phase 1 will NOT ship checkpointer-only; durable execution from Day 1 (reverses CL-33) | Standing |
| CL-36 | 2026-05-12 | dbos, temporal, durability, substrate | DBOS chosen over Temporal for durable execution substrate | Standing |
| CL-41 | 2026-05-12 | repos, marketing, architecture | Three-repo architecture: viabe-reports + viabe-team + viabe-marketing (deferred) | Standing |
| CL-44 | 2026-05-15 | dlt, vilpower, vodafone | Vilpower DLT entity registration: VI-1100095152 submitted, ₹5,920.89 paid | Standing |
| CL-50 | 2026-05-16 | twilio, accounts, sub-accounts, env-vars | Twilio account reused from Reports (single account); TEAM_TWILIO_* env vars | Standing |
| CL-52 | 2026-05-16 | migrations, supabase, path-a, env | Migrations applied via Path A: TEAM_SUPABASE_SECRET_KEY from .env.local (dev) | Standing |
| CL-55 | 2026-05-16 | l1, knowledge-graph, age, postgres, pgvector | L1 KG drops Apache AGE → Postgres + pgvector + time-aware relational | Standing |
| **CL-56** | **2026-05-16** | **observability, tracing, langsmith, logfire, pydantic, otel, dbos, cost** | **LangSmith replaced by Pydantic Logfire — aligns with DBOS OTel emission, predictable pricing** | **Standing** |
| CL-58 | 2026-05-16 | uv, ruff, tooling, openai | uv and Ruff retained — OpenAI ownership accepted; MIT-licensed | Standing |
| CL-59 | 2026-05-16 | nextjs, frontend, upgrade | Next.js upgraded 15 → 16 NOW before more code | Standing |
| CL-67 | 2026-05-17 | testing, ci, dev, twilio-sandbox | Dev testing architecture: 3-tier (CI / synthetic webhook / live Twilio sandbox) | Standing |
| CL-81 | 2026-05-18 | schema, migrations, path-first, orchestrator | Schema migrations are path-first (orchestrator-needs-first), not canonical-8-upfront | Standing |
| CL-82 | 2026-05-18 | rls, guc, postgres, tenant-isolation, app_current_tenant | RLS canonical mechanism is session GUC, not auth.jwt. **SUBSTRATE NOTE (Cowork 2026-05-26):** active impl uses `app_current_tenant()` helper from `migrations/000b_rls_helpers.sql` reading `app.current_tenant` GUC, set by `tenant_connection()` wrapper. Standing-decision wording references `app.current_tenant_id` — substrate is the source of truth. Surfaced by CC at VT-175 STEP-0; my VT-175 brief example was a literal-wording transcription error. Brief authors: cite the helper, not the literal GUC name. | Standing |
| CL-88 | 2026-05-18 | rls, guc, jwt, dual-mechanism | CORRECTION to CL-79: dual RLS mechanism (GUC for backend, JWT for client direct reads) | Standing |
| CL-97 | 2026-05-17 | env, secrets, rename, pre-merge | Env-rename PR ritual: ALWAYS pre-merge double-set; never atomic-swap | Standing |
| CL-98 | 2026-05-18 | env, secrets, rename, pre-merge | env-rename PRs use pre-merge double-set ritual (codified) | Standing |
| CL-107 | 2026-05-18 | error-handlers, founder, internal-logging | error/failure handlers do not message the founder. Internal logging only | Standing |
| CL-118 | 2026-05-18 | briefs, format, single-block, claude-code | Claude Code briefs delivered as single copyable fenced block, not split-prose | Standing |
| CL-130 | 2026-05-18 | langgraph-supervisor, deprecated, api | VT-3.4 spec used outdated langgraph-supervisor API kwarg | Standing |
| CL-132 | 2026-05-18 | merge, branch, main, pr | STANDING: all VT-* PRs target main (no `dev` branch) | Standing |
| CL-133 | 2026-05-18 | orchestrator-agent, prompt, wiring, scope | Orchestrator-agent prompt only describes behaviors actually wired in current PR | Standing |
| CL-137 | 2026-05-18 | deprecated, eol, packages, hygiene | Phase 1 codebase must not run on deprecated APIs, EOL packages, or outdated tools | Standing |
| CL-175 | 2026-05-19 | langgraph, types-command, supervisor, handoff | Drop langgraph_supervisor library. Use langgraph.types.Command directly. Supersedes CL-26/32/136 | Standing |
| CL-191 | 2026-05-18 | vt-3.4, bundle, l1, l2, fallback | VT-3.4 PR 2/3 scope locked: VT-34 bundle contract + safe-empty L1/L2 fallbacks | Standing |
| CL-205 | 2026-05-19 | recalibration, operational, session-start | STANDING (operational, load-bearing at session start): recalibration codification | Standing |
| CL-220 | 2026-05-20 | ci, gate, verification, briefs | Every brief whose verification depends on CI running must first verify the gate exists | Standing |
| CL-229 | 2026-05-20 | snapshot, template, five-fields | State Snapshot template: 5 fixed fields (Critical Path / In Flight / Blocked On / Next Action / Do Not) | Standing |
| CL-240 | 2026-05-20 | vt-29, vt-3.5, webhook, scheduled-trigger | VT-29 scoped to wrap VT-3.3 webhook only; VT-3.5 scheduled-trigger reassigned | Standing |
| CL-244 | 2026-05-20 | vt-35, vt-32, hard-limit, sdk, task-id | hard-limit-enforcement = VT-35; SDK skeleton = VT-32 (correction) | Standing |
| CL-248 | 2026-05-20 | model, haiku, opus, test, production, cost | claude-haiku-4-5 is test/canary model; claude-opus-4-7 is production | Standing |
| CL-249 | 2026-05-20 | sales-recovery, agent, anthropic-sdk, messages-api | sales-recovery agent built on Anthropic Messages SDK (pure Python) | Standing |
| CL-252 | 2026-05-20 | merge, admin-bypass, gh-pr, phase-1 | admin-bypass merge (`gh pr merge --admin`) is the standing merge method for VT-* PRs in Phase 1 | Standing |
| CL-260 | 2026-05-20 | campaignplan, v1, contract, discriminated-union | CampaignPlan v1.0 is the SOLE CONTRACT on main; v0.1 retired | Standing |
| CL-265 | 2026-05-21 | vt-50, self-evaluator, protocol | VT-50 tool return type conforms to VT-36's lean SelfEvaluator Protocol | Standing |
| CL-266 | 2026-05-21 | vt-50, vt-5.1, dependency, brief-first | VT-50 blocked on VT-5.1: Path 1 (brief VT-5.1 first) | Standing |
| CL-267 | 2026-05-21 | sprint-2, sr-agent, sequence, vt-4 | Canonical Sprint 2 SR-agent sequence; VT-4 is 6/8 done; VT-135 sibling | Standing |
| CL-268 | 2026-05-20 | draft_message_variants, deferred, llm-tools | draft_message_variants DEFERRED — not in v1. v1 LLM-backed tool set stays at 2 | Standing |
| CL-269 | 2026-05-21 | draft_message_variants, phase-1.5, deferred | draft_message_variants DEFERRED to Phase 1.5 (Fazal-locked) | Standing |
| CL-274 | 2026-05-21 | canary, two-mode, haiku-plumbing, opus-production | Two-mode canary pattern: Haiku plumbing (default, iteration) vs Opus production-fidelity (pre-merge) | Standing |
| CL-278 | 2026-05-21 | self-evaluate, revise, retry, fazal | self-evaluate gate REVISE contract — all-reasons verdict, exactly one retry carry | Standing |
| CL-281 | 2026-05-21 | self-evaluate, verdict-model, wiring | Item 4 — fold verdict-model widening into the wiring subtask (Option 2) | Standing |
| CL-284 | 2026-05-21 | dispatch-switch, supervisor, scope, closure | dispatch-switch subtask scope locked — closure swap + supervisor.py v0.1→v1.0 path | Standing |
| CL-322 | 2026-05-22 | discipline, rule-12, briefs, verify-bodies | DISCIPLINE RULE #12: verify row BODIES not just titles before escalating | Standing |
| CL-324 | 2026-05-22 | memory, l1, l2, l3, mem0, hand-built, rule-13 | Memory substrate split — L1 hand-built; L2/L3 Mem0 deferred. DR #13: stack decision not done until materialized. Supersedes CL-57 | Standing |
| **CL-330** | **2026-05-22** | **owner_inputs, structured-intent, twilio-body-drop, retention, privacy-notice, meta-terms** | **owner_inputs stores STRUCTURED INTENT (not raw bodies); Twilio Body-drop preserved; lifetime-of-relationship retention; privacy notice pending Fazal sign-off; Meta-terms pre-flight mandatory** | **LOCKED Standing** |
| CL-342 | 2026-05-22 | owner_inputs, llm-transmission, meta, anthropic | owner_inputs LLM-transmission permitted under both Meta and Anthropic terms | Standing |
| CL-374 | 2026-05-23 | compliance, anthropic-dpa, twilio, whatsapp, zdr | Three compliance items CLOSED — Anthropic DPA, Twilio/WhatsApp verified, ZDR deferred | Standing |
| CL-376 | 2026-05-23 | vt-146, owner_inputs, feature-flag, merged | VT-146 owner_inputs extraction-writer merged behind disabled flag (PR #47/#48) | Milestone |
| CL-386 | 2026-05-23 | discipline, rule-14, reconcile, ground-truth | DR #14: every status summary / handoff reconciled against ground truth (`gh pr list --state merged` + log files) before trusted | Standing |
| CL-389 | 2026-05-23 | privacy-notice, system-level, launch-gate | Privacy notice is SYSTEM-LEVEL / product launch-gate (NOT sub-task of owner_inputs) | Standing |
| **CL-390** | **2026-05-23** | **privacy, anthropic, twilio, voyage, dbos-hold, consent, voyage-raw-bodies, owner_inputs-on, july** | **LOCKED: (1) Anthropic+Twilio+Voyage+DBOS-hold MANDATORY consent-gated, baked into policy. (2) Voyage receives raw bodies. (3) owner_inputs ON for July (verify-first). (4) Privacy notice system-level** | **LOCKED Standing** |
| CL-407 | 2026-05-24 | snapshot, latest, vt-4-shipped, owner_inputs-next | LATEST STANDING STATE SNAPSHOT — 2026-05-24 close. Compressed 5-field form at `docs/clau/latest-snapshot.md` | Standing |
| **VT-269** | **2026-06-01** | **dpdp, owner_inputs, anthropic, vision, sub-processor, consent, privacy** | **Fazal DPDP ruling: owner_inputs is a SUFFICIENT lawful basis for AI sub-processor transmission of customer PII (ledger images → Anthropic). NO separate consent flag / notice required; vision prod-enablement gated only by CL-422. Complements CL-342 + CL-374; privacy-policy framing = VT-272** | **Fazal Standing** |
| **DR-15** | **2026-05-25** | **canary, rule-15, real-api, fail-not-skip, vendor-approvals** | **CANARY MANDATORY: real API + verify response + fail (not skip) on error. APPROVED without canary plan is a discipline violation. Triggered by VT-101 / PR #56 mocks-only shipping** | **Fazal-issued Standing** |
| **DR-16** | **2026-05-26** | **brief-ready, ledger-check, cl_decisions_checked, active-context-summary, rule-16** | **PRE-BRIEF-READY ACTIVE-CONTEXT CHECK MANDATORY: Cowork runs scripts/check_brief_against_ledger.py + adds cl_decisions_checked frontmatter. CC bounces missing-field signals.** | **Fazal-issued Standing** |
| **CL-416** | **2026-05-26** | **retention, pipeline-observability, lifetime-of-relationship, dsr-purge, vt-185, vt-122, supersedes-cl-21** | **PIPELINE-OBSERVABILITY RETENTION = LIFETIME-OF-RELATIONSHIP for pipeline_runs/pipeline_steps/phone_token_resolutions. NO time-based deletion. DSR-purge is sole deletion path. Supersedes CL-21 gap. VT-185 reframed v1.0→v2.0 (DSR-purge coverage). Privacy notice must disclose lifetime retention.** | **Fazal LOCKED Standing** |
| **CL-417** | **2026-05-26** | **α-sequencing, vt-187, vt-180, schema-normalization, canonical-schema-guardrail, jsonb-interim, pipeline-observability** | **(a) α-SEQUENCING: VT-187 schema normalization lands BEFORE VT-180 writer. (b) CANONICAL SCHEMA GUARDRAIL: design-doc §2.1 per-field shape is canonical; JSONB envelopes (trigger_payload, terminal_state_metadata) are interim only; no new envelope-only paths post-VT-187. Missing per-field columns (parent_step_id, tokens_input/output, status, step_name) are load-bearing for Agent SDK / cost accountability / Ops UI replay.** | **Clau-recommended + Fazal-locked Standing** |
| **CL-418** | **2026-05-26** | **discipline, rule-17, cc, git-stash, untracked, merge-task, shared-git-index, working-tree** | **DR #17: CC must not run `git stash --include-untracked` (or `-u`) during merge tasks. If working tree obstacle, CC signals Cowork + waits. Companion (VT-30 carryforward): CC uses explicit `git add <files>` (whitelist), NOT `git commit -am`. Triggered by VT-30 sweep + VT-178 stash-sweep recurrence. Single shared git index across Fazal + Cowork + CC + Claude chat.** | **Fazal-issued Standing** |
| **CL-421** | **2026-05-29** | **integration-agent, connectors, oauth, zero-manual-paste, vt-212, vt-222, sheet-redesign, apps-script-deprecated, customer-shape** | **ALL INTEGRATION-AGENT CONNECTORS MUST BE ZERO-MANUAL-PASTE AFTER OAUTH. No Apps Script paste, no copy-paste secrets, no developer-shaped setup steps. OAuth grant + auto-configuration via vendor API is the only acceptable flow. VT-212 walk surfaced Apps Script as customer-hostile (Tier-2/3 Indian SMB owner ≠ developer). Sheet connector pivots to Drive Push Notifications (Files.watch) primary + 10-min polling fallback (filed as VT-222, blocks VT-207). Shopify (VT-213) already conforms. `setup_push` + `apps_script_template` deprecated; VT-222 removes from happy path.** | **Fazal-issued LOCKED Standing** |

## Superseded entries (kept for historical greps)

| CL | Replaced by | Topic |
|---|---|---|
| CL-1 | (Notion FULLY read-only archive; no exceptions per Fazal 2026-05-26) | Tooling: PM tool. **SUBSTRATE NOTE (Cowork 2026-05-26):** Initial 2026-05-25 migration covered ViabeTeam_Sprint + Clau_Session_Log; `Viabe_Launch_Tracker` (45 milestones) was NOT in scope and remained Notion-live. Fazal directive 2026-05-26: zero Notion read-write deps; Launch Tracker migrates to `.viabe/launch-tracker/MS-*.md` under VT-177. After VT-177 merges, Notion is fully archival — any new code or task spec that adds a Notion read or write is a violation. |
| CL-20 | CL-385/CL-389/CL-390 cluster | Privacy: phone-tokenize, rest-plaintext |
| CL-21 | CL-390 cluster, now CL-416 explicit | Privacy: 90-day step-record retention. **SUCCESSOR NOW EXPLICIT (CL-416, 2026-05-26):** lifetime-of-relationship retention; DSR-purge is sole deletion path. VT-185 reframed v1.0→v2.0. |
| CL-32 | CL-175 | langgraph_supervisor library choice |
| CL-33 | CL-35 / CL-36 | Phase 1 checkpointer-only durability |
| CL-57 | CL-324 | Mem0 OSS for L1-L3 substrate |
| CL-177 | CL-260 | CampaignPlan v0.1 contract |
| CL-307/309/317/325/375/391/394 | CL-407 | State snapshots (superseded by latest) |

## How Cowork uses this file

**Before queueing any brief-ready signal** (Rule #16, mandatory):

```bash
python3 scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md
```

The script extracts domain keywords from the sprint file, greps THIS summary, and prints every active row whose tags overlap. Cowork adds a `cl_decisions_checked: [CL-N, ...]` frontmatter field to the brief-ready signal listing every row surfaced (NOT only the ones reconciled). Claude Code bounces brief-ready signals missing this field.

**While reading anything (briefs, signals, snapshots):** Cowork keeps this summary mentally cached. If a brief mentions something that should map to a row here and doesn't, Cowork pauses and reads the source CL entry.

## How Cowork maintains this file

**Update trigger events** (any of these = update this file in the SAME action):
1. A new CL entry is logged that has Standing or LOCKED status.
2. An existing CL entry is superseded by a new one.
3. A Fazal directive lands that changes strategy / scope / discipline.
4. A merge to main closes a phase (e.g., owner_inputs verification shipped).
5. A discovery during a brief-ready check that a CL entry should be summarized here but isn't (i.e., the sweep TODO catches one).
6. A brief is queued whose `cl_decisions_checked` field surfaces a CL that needs better tagging here.

**Pruning rule:** when a row's claim is no longer in force (phase closed, decision superseded, work completed and archived), MOVE it from the active section to the `## Superseded / completed` table at the bottom. Don't delete — historical greps still want the row, but it shouldn't pollute the active context.

**Tagging discipline:** over-tag, never under-tag. The script's failure mode is missing a relevant CL; an extra tag costs nothing.

## Sweep TODO — full coverage pass over `entries/CL-*.md` + sprint briefs

This summary currently covers only Standing decisions extracted from `docs/clau/decisions-ledger.md`. The full sweep over all 369 `docs/clau/entries/CL-*.md` files + 167 `.viabe/sprint/VT-*.md` briefs is pending.

**What the sweep produces (one batch of rows per topic):**
- Every CL entry whose claim is still active gets a row here, in the appropriate topic section above.
- Every sprint brief that defines a contract still in force (e.g., the `pipeline_log` event-type schema in VT-102, the cost-per-paise rounding rule in VT-103, the bank-account narrowing decision in VT-104) gets a row here citing the brief.
- Deferred / Phase-2 / Phase-3+ items get a one-liner in the Superseded/completed table below with the reason ("Phase 2: ML name detection" — not in scope until X).
- Completed-and-archived items (e.g., VT-3.x DBOS substrate, VT-17 repo bootstrap) get a one-liner confirming the closure.

**Sweep batches (do in this order, when time permits — not blocking VT-171 hot-fix or VT-28 plan):**

| Batch | Source | Est. CL count | Done? |
|---|---|---|---|
| 1 | `decisions-ledger.md` Standing decisions | ~70 | ✅ done (initial extraction 2026-05-26 04:20 IST) |
| 2 | Recent CL entries 2026-05-22 onwards (CL-300 to CL-415) — covers post-cutover session work | ~115 | ⬜ pending |
| 3 | Sprint 1 brief contracts (VT-101/102/103/104/121/28/30/125/126 + VT-120 Fazal-owned) | ~10 | ⬜ pending (partially captured via VT-104 follow-up VT-170) |
| 4 | VT-3.x DBOS substrate + VT-31/32/33-39 contracts | ~10 | ⬜ pending |
| 5 | Privacy cluster CL entries (CL-330, CL-389, CL-390 already captured; cross-refs pending) | ~15 | ⬜ partial |
| 6 | Pre-cutover CL entries 2026-05-12 to 2026-05-21 (CL-1 to CL-299) — bulk of historical context | ~169 | ⬜ pending (lowest priority — most already in `decisions-ledger.md`) |

**Time estimate:** ~2-3 hours for batches 2-6 combined. Do in chunks between active task work, not in one block.

## Maintenance log

| When | Who | What changed |
|---|---|---|
| 2026-05-26 04:20 IST | Cowork | Initial extraction from `decisions-ledger.md` (batch 1) — 70 Standing rows + 7 superseded. Triggered by CL-56 LangSmith→Logfire drift incident. |
| 2026-05-26 04:35 IST | Cowork | Pivot per Fazal: renamed `ledger-index.md` → `active-context-summary.md`; reframed as Cowork-maintained digest (not "external ledger"); added Sweep TODO + maintenance log. |

- **CL-422** (2026-05-29, STANDING with launch-gate sunset) — Dev DB Seoul-accepted (free-tier); prod = Mumbai (VT-231 launch-blocker); hard constraint: no real customer data on dev until prod ships.
- **CL-426** (2026-06-01, Fazal-issued, STANDING) — **VTR = decaying human-on-the-loop**, not a permanent gate. Agent operates; VTR is the escalation target for business-KNOWLEDGE gaps + monitors daily/event activity. Fazal = VTR #1 (first cohort). KG-injection = accelerant (resolutions feed back → flywheel); independence = MEASURED decay threshold, not a date. Three-way routing (LOCKED): agent-confident acts · VTR resolves knowledge gaps · OWNER holds authority (Pillar 7). **Customer PII ENCRYPTED FROM VTR** — de-identified/business-level only; identity-needing escalations → owner. Instrument escalation-rate/decay from day one. Agent-op rows: VT-279/280/281/282; multi-VTR console → VT-189. Tags: vtr, human-on-the-loop, escalation, routing, pii, kg-injection, onboarding, autonomy.
- **CL-427** (2026-06-02, Fazal-issued, STANDING) — **Connector-audit gate + CL-421 correction.** Shopify does NOT yet conform to CL-421 zero-paste: the shipped path (VT-208/#221) is client_credentials = dev/same-org ONLY; the owner-facing OAuth managed-install is **VT-283 (plan-first)** — Shopify conforms only once it ships (connector exposes BOTH modes). STANDING: every new connector must pass "owner ONLY approves — no app-creation/scope-screen/secret-paste/dev-step" BEFORE reaching owners (ref docs/diagrams/viabe_connector_audit_16x10.png). Ease follow-ups: VT-284 (UPI forward-to-WhatsApp), VT-285 (POS OAuth/upload). Tags: connector, oauth, cl-421, zero-paste, shopify, audit, owner-flow.
