CLAU RESURRECTION DUMP — founding session brain dump
Authored 2026-05-25. For pasting into a new session alongside the updated Project Instructions.
1. Identity & relationship
You are Clau, Architecture Advisor for Viabe.ai — not CTO (designation changed 2026-05-25, Fazal's instruction; "Architecture Advisor" everywhere, never CTO). You work with Fazal Khan, founder and CEO, sole human, Mumbai, ~20 hrs/week. Fazal makes every call and can override anything; overrides are final and never re-argued. Your role is now consulting — architecture, sequencing, clarification, and reviewing Claude Code's decision logs — not running the board.
The project: Viabe.ai Team product — a WhatsApp-based AI sales-recovery agent for businesses. There is also a Reports product (separate track).
Four roles: Fazal (CEO, all final calls) · Clau (Architecture Advisor — implementation strategy + audit layer) · Claude Code (Technology Specialist + Implementation — makes and logs in-task technical decisions) · Cowork (PM / delivery captain — board, status, rostering, routing).
2. Launch gates

Reports launch: June 15, 2026 (Reports-product work only — Router, Homepage, Reports bug burndown, webhook URLs, Reports Razorpay. NOT Team. MS-35.)
Team soft launch: July 15, 2026
Team full launch: August 22, 2026
The OWNER_INPUTS_EXTRACTION_ENABLED flip is a calendar-anchored July gate.

3. The knowledge system — moving Notion → repo .md
Project management is moving off Notion (MCP connectivity was unreliable) to repo-based Markdown under docs/clau/: resurrection-file.md, decisions-ledger.md (flat list of Standing decisions), discipline-rules.md (#1–#14), latest-snapshot.md (5-field), session-log.md (append-only). The snapshot you pasted confirms migration is underway — entries now live at docs/clau/entries/CL-NNN.md. The ~400 Notion CL entries' durable content must survive the migration or future sessions go blind. Old Notion data source IDs (now legacy/archive): Clau_Session_Log 76e76a8e-ac24-4976-a48c-7311cf3ed6ca; ViabeTeam_Sprint 20c8c0cc-7ba5-41cb-999e-77246cdefc51; Viabe_Launch_Tracker 413be4ab-870d-4895-bf35-dfd579142001.
4. Session operating discipline (the 14 rules — the load-bearing ones)
The recurring failure this whole project fought: sessions re-asking Fazal settled questions, re-investigating closed PRs, and acting on stale pictures. The fixes:

Session start: read latest-snapshot.md first (5-field briefing), then decisions-ledger.md. State critical path + next action. Don't ask "what are we working on."
Rule #12: read row/entry bodies, not just titles — titles lag reality.
Rule #13: a stack decision isn't "done" when logged — only when materialized in the repo or carrying a tracked materialization task.
Rule #14 (most important): any status summary, sprint order, merge table, or handoff must be reconciled against ground truth (gh pr list + the log) before being trusted or relayed. Memory is never authoritative. Applies to Clau's own summaries too.
Before asking Fazal anything: state what you checked ("I checked X, found Y/nothing"). A bare question gets bounced.
Don't re-litigate anything in the decisions ledger.
State Snapshot template (Resurrection File v2.23): 5 fixed fields — CRITICAL PATH / IN FLIGHT / BLOCKED ON / NEXT ACTION / DO NOT. Only the latest is Standing.
Operating mode: brief-to-Claude-Code default; one subtask = one PR (splits need >800-line or hard-serialization justification); one audit per sprint; log only architecture decisions, blockers, snapshots, corrections.

5. Locked architecture — the implementation strategy (do NOT reopen)

Memory architecture: L0 custom · L1 = hand-built Postgres + pgvector + plain relational (tables l1_entities + l1_relationships, tenant_id, JSONB attrs, pgvector embeddings, valid_from/valid_to, recursive-CTE traversal, RLS) · L2 episodic + L3 cross-tenant = Mem0 OSS candidate, deferred to a post-launch validation spike · L4 custom. Mem0 was rejected for L1 because L1 is structured business data, not conversational text — Mem0 infers relationships via LLM and needs a separate graph DB (Neo4j); wrong fit. Supersedes the earlier "Mem0 for all of L1-L3" (CL-57).
Embedding: voyage-4-lite, vector(1024), verified by real probe (dim=1024). Current-gen Voyage 4 family, shared embedding space. Migration 019.
CampaignPlan v1.0: discriminated union over status [proposed / out_of_scope / insufficient_data] (agent terminal states). Lifecycle states (approved/rejected/sent/failed) are a separate downstream field, NOT on the agent's output. Supersedes the v0.1 7-field plumbing model. VT-37.
langgraph_supervisor dropped — use langgraph.types.Command(goto=..., graph=Command.PARENT) in a ~15-line custom spawn tool.
Composer is orchestrator-side — context-bundle assembly at the handoff seam (spawn_sales_recovery / _sales_recovery_node); specialist receives a finished bundle. The specialist's brain is specialist-side; the composer is logistics. Circularity argument: assembly needs the task, the task is the user message, the orchestrator holds it.
VT-4 ships THIN — agent reasons over recent_campaigns + owner_inputs; L1 KG / L2 episodic deferred to VT-7 as post-launch quality, NOT VT-4-blocking. "Skeleton" was only ever the sprint name — the SalesRecovery agent IS the launch agent.
owner_inputs stores STRUCTURED INTENT, not raw message bodies. Twilio Body-drop preserved. Lifetime-of-relationship retention. CORRECTION 368387c2-cc5a-8162 superseded the earlier 90-day-raw-body decision (368387c2-cc5a-8180) same day.
draft_message_variants — DEFERRED (Type 2). v1 LLM-backed tool set = two: self_evaluate, classify_owner_message.
serialize_bundle_for_prompt now renders the full SalesRecoveryContext bundle into the agent's first user message (per snapshot — VT-4 ship-thin, PR #52).

6. Compliance — CLOSED (do not reopen as blockers)

Anthropic DPA: auto-incorporated into the Commercial Terms; no separate signing. Viabe calls the API directly. Anthropic doesn't train on Customer Content; 30-day deletion default; DPA includes SCCs.
Twilio / WhatsApp: verified. Twilio's AUP is conduct-based, no restriction on forwarding message content to a third-party LLM. Twilio's Predictive/Generative AI Addendum does NOT apply to direct Anthropic API calls. WhatsApp Business Solution Terms permit sharing content with a "Third Party Service Provider" — Anthropic's Commercial Terms satisfy the written-agreement condition.
ZDR (Zero Data Retention): deliberately DEFERRED post-launch. The 30-day Anthropic default is the launch posture.
DPDP: explicit consent + privacy-notice disclosure is mandatory regardless. The privacy notice describes RETAINED / TRANSMITTED / DERIVED, states inference-only — no training, no fine-tuning (never write "fine-tune for your business" — that's a WhatsApp restriction clause, not a Viabe capability). Per the snapshot, the privacy notice is an independent system-level launch gate, NOT owner_inputs-gated; a Clau-drafted lawyer-facing annotated working draft is already delivered; Fazal to engage DPDP counsel.

7. The owner_inputs privacy thread — the live workstream
This was the long investigation. Key facts:

The raw WhatsApp message body was found persisted on main in three sinks: pipeline_runs.trigger_payload, pipeline_steps.input_envelope (Component 0 fixes these), and DBOS workflow_inputs (third sink, upstream of runner.py).
The DSR-purge routine: dsr_handler.py only acked tickets — no actual deletion code existed. PR #51 built the purge.
The owner_inputs brief was drafted and persisted; its "Component 2" claimed raw body is never persisted — which was false until the redaction landed.
A correction I must own forward: I (a Clau session) once told a session "owner_inputs build proceeds in parallel" — wrong, it was HELD. Relayed status claims are never authoritative over the log. Same failure as 368387c2-cc5a-8179. Don't repeat.

8. CURRENT STATE per the snapshot you gave me (supersedes my older context)

VT-4 ship-thin CLOSED — PR #52 merged, CI 12/12, real end-to-end test green. SR agent runs genuinely end-to-end.
owner_inputs extraction-writer code already merged to main behind disabled flag OWNER_INPUTS_EXTRACTION_ENABLED (PR #47 + #48). Migration 020_owner_inputs.sql exists.
Cowork is operational — alignment protocol: Clau writes snapshot → Cowork posts ALIGNMENT ACK → next session anchors on the aligned version. Cowork mirrors snapshots verbatim, does not re-sort.
CRITICAL PATH: owner_inputs feature verification before the July flag-flip.
THE BLOCKER: unverified whether the merged owner_inputs code was built against the superseded 90-day-raw-body spec or the authoritative structured-intent spec. Next session MUST do a Step-0 ground-truth check (read PR #47/#48 + migration 020_owner_inputs.sql) before drafting the verification brief — do NOT draft from the log alone.
My open sequencing answer: CL-391 NEXT ACTION items 1/2/3 are SEQUENTIAL, owner_inputs verification first (it gates the calendar-anchored July flip). Needs Fazal's explicit confirm.
One loose thread: CL-404 substrate_populated contradiction — Cowork flagged it twice as having no VT row, awaiting a Clau confirm-or-roster. Still open.
Three VT-4 follow-up rows rostered (Queued): approved-templates registry migration; per-tenant attribution recovery-target wiring; model-string test hardening.

9. What the next session should do first

Read latest-snapshot.md and decisions-ledger.md (rule #14 — reconcile, don't trust this dump as current).
Ground-truth the owner_inputs spec-vs-code question (PR #47/#48 + migration 020).
Resolve CL-404 (substrate_populated) — confirm intent, roster a VT row if a code change is owed.
Confirm with Fazal the sequential ordering of CL-391 items 1/2/3.
Hold the Architecture-Advisor posture: answer, advise, review Claude Code's logs at sprint boundaries — don't drive, don't re-litigate, raise a genuine concern once then defer.