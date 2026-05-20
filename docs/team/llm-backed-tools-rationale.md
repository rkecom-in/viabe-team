# LLM-Backed Tools — Rationale (VT-39)

Pillar 2: most tools are deterministic. The few that ARE LLM-backed
must be justified — they cost more, return non-deterministic outputs,
and broaden the system's failure modes. This doc enumerates the locked
LLM-backed tools and the criterion they satisfy.

## Criterion

A tool may be LLM-backed only when:

1. The task is **semantic** — pattern matching, paraphrase tolerance,
   or nuanced classification that a deterministic rule cannot cover
   without an unreasonable rule explosion.
2. The deterministic alternative was tried OR is provably worse — false
   negatives at a rate that erodes owner trust, or maintenance cost
   that scales worse than the LLM cost.
3. The output schema is **structured + typed** — the LLM emits JSON
   conforming to a Pydantic model, not free prose the rest of the
   pipeline must interpret.
4. The model has a known cost ceiling and the call is **counted** by
   `VT-35`'s tool-call cap (25/run).
5. The model and its cost rationale are documented in this file.

Tools that don't satisfy all five MUST be deterministic. The framework
defaults to `is_llm_backed=False`; the override is rare.

## Locked LLM-backed tools

### `self_evaluate` — Opus 4.7

**Status:** locked. Used by VT-36 (self-evaluate gate). Implementation
pending VT-50.

**Why LLM not deterministic:** the four evaluation categories —
schema, pillar discipline, internal consistency, legal compliance —
are semantic. Schema violates a Pydantic model can catch directly, but
semantic schema issues ("cohort_size=200 but customer_ids has 87
entries — they don't match across fields") are easier for an LLM to
flag. Pillar discipline and consistency are nuanced. Legal compliance
needs paraphrase tolerance for high-pressure language. The
deterministic alternative — keyword lists — false-negates on novel
phrasings and costs maintenance.

**Why Opus not Sonnet / Haiku:** the gate's job is to catch false
negatives (a draft that should be revised passes through). False
negatives cost owner trust irrecoverably; the cost difference between
Opus and Sonnet is small relative to the cost of a bad campaign
leaving the system. Run-level expected cost: ~₹10-15 per evaluation,
~50 evaluations / day, ~₹500-750 / day — within the cost ceiling.

**Rationale citation in code:** override site at the seam (VT-50, when
it lands) MUST point back here. The VT-36 gate test
`test_evaluation_criteria_are_the_four_documented` locks the four
categories.

### `classify_owner_message` — Opus 4.7

**Status:** locked. Used by the orchestrator's inbound classifier.
Implementation pending VT-5.x.

**Why LLM not deterministic:** owner inbound is free-text in Hindi,
English, and mixed-language Hinglish. The classifier maps to a small,
typed action set (acknowledge / approve / reject / ask-question /
out-of-scope). The deterministic alternative — regex / keyword
matching — false-negates on natural-language variation and on
mixed-language input.

**Why Opus not Sonnet / Haiku:** misclassification on owner intent is
high-blast-radius (e.g. classifying an approval as a rejection
silently kills a campaign). Run-level expected cost: ~₹3-5 per
classification, batched by inbound frequency — within budget.

**Rationale citation in code:** override site MUST point back here.

## Phase 1.5 deferral (Fazal Type 2 decision)

### `draft_message_variants`

`draft_message_variants` — DEFERRED to Phase 1.5 per Fazal Type 2
decision 2026-05-20 (Notion page
`366387c2-cc5a-8159-b726-e7ce3ec6f4f3`). Not in the v1 LLM-backed set.

The v1 LLM-backed set is exactly two: `self_evaluate` and
`classify_owner_message` (above). The framework's `is_llm_backed()`
flag + `llm_backed_in_subset` registry audit remain decision-agnostic
— if `draft_message_variants` lands in Phase 1.5, the entry gets
promoted to "Locked LLM-backed tools" above with its own
why-LLM + why-Opus + cost ceiling, no framework change required.

## Audit

`tool_registry.llm_backed_in_subset(subset)` returns the LLM-backed
tools in any subset — used by:

- the system-prompt generator (VT-33) to enumerate Opus-backed tools
- the cost-budget pre-flight (per-run estimate)
- this doc's "currently locked" appendix at sprint review time

## Evolution

Adding an LLM-backed tool is **Type 2 governance** — Fazal + Clau
sign-off and a brief. Promoting a Sonnet/Haiku tool to Opus is also
Type 2. Demoting (Opus → Sonnet) requires a measured failure-rate
study; the model in `models.yaml` is the source of truth.
