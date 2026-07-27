# LANGUAGE — per-tenant owner-language preference (design, 2026-07-18)

Design-first per Fazal (item #2, NEXT BUILD). Produced by CC design agent, code-grounded. AWAITING
FAZAL RATIFICATION of 3 decisions (below); everything else CC-defaultable.

## Premise correction (code truth)
The schema HAS per-tenant language columns since migration 001 (`migrations/001_tenants.sql:8,16`):
- `tenants.language_preference` TEXT NOT NULL DEFAULT 'en' — dead default; nothing writes it.
- `tenants.preferred_language` TEXT nullable — written ONCE at signup (`onboarding/signup.py:154-164`,
  validated en|hi), sourced from the signup form's EN/HI UI toggle (a display-language PROXY, not an
  asked question).
~8 consumer sites hand-roll `COALESCE(preferred_language, language_preference, 'en')`:
freeform_acks.py:55 (resolve_owner_locale) · monthly_report.py:157 · onboarding_conductor.py:603 ·
runner.py:454 → output_composer.py:154 · stale_resume.py:89 · get_business_profile.py:95 ·
twilio_send.py:381 (welcome EN/HI, VT-393) · team-web lib/i18n.ts.

**Real gap:** the preference governs only DETERMINISTIC surfaces. The conversational brain ignores it
(prompt hard-requires per-turn MIRRORING, orchestrator_agent_system.md:17-23; same in turn_brain.py:92
+ dispatch compose fallbacks). The brain's business-context block does NOT carry it. And the value
space is en|hi only — Hinglish (hi-Latn) has NO stored representation.

## Core precedence rule
**live-turn mirroring > persisted preference > 'en'.** Replies MIRROR the owner's turn (unchanged —
judge-validated; don't fight it). The persisted preference governs AGENT-INITIATED messages (welcome,
nudges, stale_resume, monthly report, acks, template variants) + ambiguous turns (emoji/one-word) as
a brain fallback hint.

## Recommendations (a-e)
- **(a) Capture — layered, no new onboarding question.** `preferred_language` = EXPLICIT owner choice
  (NULL until a real choice); `language_preference` = OBSERVED rolling value (signup toggle demoted to
  seeding it). Inference: add `language: en|hinglish|hi` enum to the EXISTING triage classify output
  (TriageResult, manager/triage.py:71 — zero marginal LLM cost; enum = finite outcome, compliant with
  the no-keyword-lists standing). Devanagari codepoint detection deterministic-overrides to 'hi'.
  Explicit verbal choice ("English only") → new manager tool `set_language_preference` writes
  `preferred_language`.
- **(b) Storage — the EXISTING tenants columns.** Operational send-path config, RLS-scoped SQL at
  transport time; business_profile (L1, LLM-curated) is wrong durability. Likely NO migration needed
  (TEXT, no CHECK). Optional: CHECK + comments migration via the allocator (needs data-normalization
  guard first).
- **(c) Enforcement — prompt-side for conversation; deterministic ONLY for variant selection. NO
  post-check/rewriter** (can't fix wrong-language without a second LLM call; would fight mirroring).
  Changes: preference line into the business-context identity block + ONE scoped sentence in the
  system prompt (ambiguous-turn fallback) + judge criterion (measure, don't enforce) + consolidate
  the 8 COALESCEs onto one canonical resolver (promote freeform_acks.resolve_owner_locale).
- **(d) Per-message override — follow the turn for the reply; never rewrite the explicit preference.**
  A one-off English message nudges the OBSERVED column only.
- **(e) Customer sends — already separate; keep + guard.** CampaignPlan.message_plan.language is
  per-cohort (SR prompt: "matching the cohort, not the owner"). Add a conformance test asserting
  campaign language is never sourced from the tenants columns.

## Risks
- System-prompt + context-block edits touch the money-path brain: variance-dominated — x3 full-journey
  re-drive on deployed dev before trusting; keep the prompt diff to ONE sentence.
- CHECK migration can break on existing rows — normalize first or skip.
- Signup-write retarget: one-line backfill for real tenants (harness tenants recreated).

## FAZAL DECISIONS (3) — blocking the build
1. **Hinglish register for agent-INITIATED sends:** Hinglish owner's templates/acks render Devanagari
   'hi' (CC default — warmer) vs English vs paying for hi-Latn Meta template variants. Brand/voice.
2. **Does explicit preference ever override live mirroring?** CC recommendation: NEVER — explicit
   governs agent-initiated only; a hard override reads broken on WhatsApp. Semantic call.
3. **Explicit onboarding language question:** CC recommendation NO (friction; toggle + inference +
   verbal override suffice). Onboarding UX is Fazal's.

## Touch list (build phase, after ratification)
canonical resolver module + 6 caller refactors · triage.py language enum + inbound persist ·
business_context.py identity line · orchestrator_agent_system.md one sentence · turn_brain.py hint ·
agent/tools/set_language_preference.py + registry · signup.py seed retarget · optional migration via
allocator · VT row via vt_id_allocate · tests + judge criterion + campaign-conflation guard.
