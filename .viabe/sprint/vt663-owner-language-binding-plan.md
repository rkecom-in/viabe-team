---
id: VT-663
title: Owner-language binding — bind the manager brain to the per-tenant language + runtime inference
status: design-complete
priority: High
exec_order: 1
owner: claudecode
created: 2026-07-17
authorization_basis: "Fazal 2026-07-16 (FAZAL-finish-open-tasks-language-arch-priority) item 2 — LANGUAGE elevated to NEXT BUILD, design-first. Live-drive evidence."
grounding: "Explore seam-map 2026-07-17 + direct verify: context_builder/orchestrator_agent_driver/orchestrator_agent grep = 0 language injects; orchestrator_agent_system.md:17-19 soft mirror; find_open_for_tenant/resolve_owner_locale/_load_preferred_language column reads."
---

# VT-663 — Owner-language binding

## Root cause (precise — corrects the "no shared state" hypothesis)
Per-tenant language state **already exists**: `tenants.preferred_language` (nullable, explicit) `?? language_preference` (NOT NULL default `'en'`), resolved `COALESCE(preferred_language, language_preference, 'en')`. The **deterministic layer reads it** (output_composer, freeform_acks.resolve_owner_locale, runner._load_preferred_language → `_RECONFIRM_SEND_PUSH["hi"]` etc). The **manager brain reply path does NOT** — `context_builder.py` / `orchestrator_agent_driver.py` / `orchestrator_agent.py` inject **zero** locale (verified: grep = 0). The brain gets only a soft prompt: "Mirror the owner's language…" (orchestrator_agent_system.md:17-19). So on "Mera top customer kaun hai?" the brain LLM-mirrored, silently fell to English; the win-back re-confirm rendered Hindi off the column. **Two layers, two sources of truth → the split Fazal saw.**

## Design

### 1. Binding taxonomy — stay BINARY at the stored/template level `{en, hi}`
`preferred_language ∈ {en, hi}` remains the Meta-renderable binding value (hi = Devanagari Hindi; en = English). **Register** (English / Devanagari-Hindi / romanized-Hinglish) is a finer distinction the BRAIN mirrors *in free-form, in-window*, but the STORED value that (a) drives templates and (b) is the brain's language floor stays `{en, hi}`. Mapping: romanized-Hinglish → stored `hi` (same language, Latin script). Keeps the existing en/hi template SIDs working; no new column, no migration.

### 2. Bind the brain to the column (the CORE fix — decision-independent)
- Inject a resolved `owner_language` (via existing `_load_preferred_language`/`resolve_owner_locale`) into the manager brain context (`context_builder`) and the compose fallbacks.
- Upgrade the prompt from soft "mirror" to authoritative: *"The owner's language is **{owner_language}**. Reply in {owner_language}. Within it, mirror their exact register (Hinglish vs Hindi script vs English). NEVER reply in a different language than {owner_language}, even on an ambiguous single message."* Apply to: `orchestrator_agent_system.md`, `_REPLY_TOOL_DIRECTIVE` (orchestrator_agent.py:105), `_COMPOSE_COMPLETION_SYSTEM` + `_COMPOSE_ANTIREPEAT_SYSTEM` (dispatch.py). The column becomes the floor; mirroring only refines register. **This alone kills the observed English-fallback.**

### 3. Runtime inference + stickiness (the genuinely-new piece)
Today `preferred_language` is set only at signup (en/hi). An owner who signed up `en` but writes Hinglish → column `en` → brain told `en` → wrong. Add at the owner-inbound seam:
- **Detect** the inbound message language → `{en, hi}` (Devanagari → hi trivially by script; romanized Hinglish → hi via the existing Hinglish-aware read; English → en). Reuse existing script/Hinglish infra (keyword_match, classify_owner_message locale, send_intent) — do NOT hardcode a keyword list for the en-vs-Hinglish call ([[no-lists-for-undefined-possibilities]]); LLM/heuristic decides register, deterministic code only for the unambiguous Devanagari-script case.
- **Sticky update** with hysteresis: null/default + first substantive message → set from inference; set + N consecutive messages in a different language → shift (N≥2 to avoid flip-flop on a borrowed word). Explicit onboarding choice = initial value; consistent behavior overrides. Deterministic + audited (write reason).

### 4. Deterministic layer — BIGGER than templates (P1-validation finding, 2026-07-17)
The Meta-template layer IS column-driven (output_composer, freeform_acks, runner reconfirm). BUT the P1 dev validation surfaced that the deterministic ANSWER NETS are NOT — e.g. `owner_inputs/status_query.py:586` returns a hardcoded English `f"You currently have {n} customers in your ledger."` regardless of owner language (a Hinglish owner's count/status ask gets an English deterministic answer even after P1, because P1 only binds the BRAIN prompts). **P2 scope therefore includes the deterministic answer nets in `owner_inputs/status_query.py` (and any sibling pre-brain nets), not just the Meta templates.** These need Hindi/Hinglish variants selected off the resolved owner-language, OR routing through a language-aware renderer.

### 5. j08 subsumption
Brain bound to `hi` + Devanagari-aware classification already present → j08 (Devanagari negation answered in-register) is covered by the core.

## THE ONE PRODUCT CALL (Fazal) — does NOT block the core
Out-of-window (>24h) sends are locked to registered Meta template SIDs, which exist only per fixed language (`en`, `hi`-Devanagari). A **romanized-Hinglish** owner messaged out-of-window therefore cannot receive romanized Hinglish — only English or Devanagari Hindi.
**Recommendation:** map Hinglish → `hi` (Devanagari) out-of-window (it IS their language; only the script differs; out-of-window sends are rare + templated). **Alternative:** force `en`. Fazal's call. The core (P1+P2) handles pure en/hi correctly regardless of this ruling — it only affects the out-of-window register for a Hinglish owner.

## Build plan
- **P0** — this design + VT-663 row + surface (done).
- **P1 (decision-independent core)** — inject `owner_language` into brain context + authoritative prompt (orchestrator_agent + dispatch compose + reply_to_owner). Fixes the observed bug. Unit + dev journey re-drive.
- **P2 (inference/stickiness)** — inbound language detect + sticky column update with hysteresis + audit. Medium (touches inbound).
- **P3 (validate)** — dev re-drive of a Hinglish-owner scenario (new/extended journey) + tier_rescore; j08 regression check; confirm money-path brain replies (approval acks) render in-language without changing gate logic.

## Risk
P1 changes manager reply language broadly but is ADDITIVE (a language constraint, not a gate-logic change). Money-path deterministic copy is unchanged (already column-driven). Validate on deployed dev, not just units ([[validate-money-semantics-on-dev]]). No new column/migration for the core.
