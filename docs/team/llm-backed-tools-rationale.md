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
shipped in VT-50 at
`apps/team-orchestrator/src/orchestrator/agent/tools/self_evaluate.py`.

**Why LLM not deterministic:** the four evaluation categories —
schema, pillar discipline, internal consistency, legal compliance —
are semantic. The Pydantic model catches direct schema violations at
parse time, but cross-field "semantic schema" issues
("cohort_size=200 but customer_ids has 87 entries — they don't match
across fields"; "cohort_label='90-180 day dormants' but
context_summary shows zero customers in that bucket") are easier for
an LLM to flag than for a rule explosion. Pillar discipline (invented
numbers, per-vertical heuristics, overstated confidence, retention
pressure) and legal compliance (paraphrase tolerance for high-pressure
language under Indian DPDPA + Twilio/Meta WhatsApp policy) both
require natural-language understanding. The deterministic alternative
— keyword lists — false-negates on novel phrasings and costs
maintenance that scales worse than the LLM cost.

**Why Opus not Sonnet / Haiku:** the gate's job is to catch false
negatives — a draft that SHOULD be revised passing through to delivery.
False negatives cost **owner trust irrecoverably**: a bad campaign that
leaves the system is hard to claw back, while a needless revise costs
one extra cycle. The cost difference between Opus and Sonnet on a
small evaluator JSON output is small relative to the blast radius of
a bad approval.

**Cost ceiling (Phase 1):**

- Per evaluation: ~₹10-15 (Opus 4.7 list price; small input + small
  output; full math in `apps/team-orchestrator/src/orchestrator/agent/cost.py`).
- Volume: ~50 evaluations / day at Phase-1 tenant scale (one or two per
  agent run, two-revise-then-fail policy bounds the runaway case).
- Daily cost: ~₹500-750 — within the LLM-backed budget.
- Demoting to Sonnet/Haiku is **Type 2 governance** and requires a
  measured failure-rate study (concept-team.md §8.4).

**Rationale citation in code:**
`SelfEvaluateTool.is_llm_backed` (VT-50) carries a one-line override
comment pointing back to this section. The VT-36 gate test
`test_evaluation_criteria_are_the_four_documented` locks the four
categories at the gate level; VT-50's
`test_input_schema_rejects_reasoning_chain` locks Pillar 7 independence
at the tool level.

**Model pin:** `apps/team-orchestrator/config/models.yaml`
`self_evaluate.production = claude-opus-4-7`. Read at runtime via
`_resolve_self_evaluate_model()`; never hardcoded in the tool.

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
