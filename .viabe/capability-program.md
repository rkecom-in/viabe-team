# The Capability Program — Team Manager to Claude-Code grade
**Authorized:** CL-2026-07-03-autonomous-capability-program (Fazal, autonomous mode, do-or-die).
**Owner:** Claude Code, autonomous. **Audit:** Cowork (audit-after). **This file = the resumable approach doc** — any future CC session or Cowork reads this first.

## The bar (Fazal's words, operationalized)
The Team Manager must handle business conversation the way Claude Code handles engineering:
1. **Context retention** — NEVER asks for what the conversation already contains (canonical failure: the owner sent his store link 3× on 2026-07-03; each seam read a different context substrate or none).
2. **Intent understanding** — natural phrasing everywhere in-session; keyword floors only for compliance (STOP/opt-out/DSR) and as fast paths.
3. **Honesty** — never claims an untaken action; never invents facts; substance-railed disclosures.
4. **Business judgment** — decides next steps, guides specialists, composes data-grounded month plans (never hollow), validates/accumulates specialist plans (CL-plan-governance).
5. **Sub-agents = specialized mini-managers** — own tools, own memory, readiness self-checks (CL-agentic-fleet-mandate).
6. **Every inbound gets a response.** Silent drops are architecture failures, not bugs.

## Root-cause thesis (why it kept failing)
Conversation ownership is FRAGMENTED across deterministic seams (journey walker, paced-flow beats, shopify resume gate, prefilter direct handlers, dispatch brain) — each owns a slice of messages with its own slice of context. Amnesia, canned copy, and silent drops are all symptoms of that fragmentation. The end-state: **ONE conversational brain (the Team Manager) owns every in-session turn**, reading ONE context substrate (conversation_log + memory + flows-as-state), with deterministic rails as gates/tools around it — flows inform the brain; they never compete with it for the microphone.

## Phases
- **P0 (in flight)** — land VT-582 (server harness: injected inbound → deployed dev brain → captured outbound; dev-only secret, mocked sends) + VT-583 wave 1 (top keyword surfaces → floor+converse; ALL silent drops fixed; context reads unified onto conversation_log). Evidence: suites + harness starter scenarios.
- **P1 — conversation unification (wave 2)** — the single-brain turn owner: journey/flow/integration state become CONTEXT for the manager brain; the walker/beat token paths demote to fail-soft floors; remaining canned copy → substance-railed composition (sweep inventory 2026-07-03 = the checklist, in keyword-surface-sweep's report, relayed to Cowork).
- **P2 — delegation + plan governance** — VT-578 (specialists propose; manager validates/accumulates/manages via the B2 spine), VT-573 (Sales Recovery tool belt + lane memory + readiness self-check), VT-572 (manager memory self-write + compaction).
- **P3 — exhaustive server-side verification** — harness scenario permutations: onboarding happy/hostile/topic-switching, consent natural-language, integration flows incl. re-sent links + mind-changes, status/plan/edit requests, stop/resume, multi-field answers, repeats. Hard asserts: no silent drop, no re-ask of in-context facts, substance rails, floors intact. Plus an opus LLM-judge rubric per transcript: context-retention / intent / honesty / helpfulness / progression, threshold ≥4/5 each. Regression: the whole pack runs before any future conversational change ships.
- **P4 — the ping** — Fazal gets transcripts + rubric scores + the change ledger, only when P3 passes.

## Standing constraints (unchanged by autonomy)
Effect gates deterministic forever (sends/allowlist, consent recording, money, DSR); main = Fazal-only; allowlist-only real sends; no real sends from harness (bogus non-allowlisted tenants; send-guard asserts); prod untouched; every wave: tests + smoke + lint + pre-push green, deploy verified, Cowork signaled.

## Model policy (this program)
Manager turn brain: sonnet (conversational hot path) with opus escalation where judgment is consequential (plan validation, entity adjudication — already opus). Builders: opus. Judge: opus. Cheap classifiers/distillers: haiku.

## Log (append per wave)
- 2026-07-03: Program started. P0 builders in flight. Ledger entries: fluid-consent, conversing-surfaces+harness, conversation-memory, plan-governance, populate-first, paced-needs-driven, fleet-mandate, autonomous-program. Sweep inventory delivered (14 keyword surfaces / ~30 canned / 5 silent-drop paths).
