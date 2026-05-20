# Self-Evaluate Criteria (VT-36)

The four categories the self_evaluate seam evaluates against, every time
the Sales Recovery agent produces a draft `CampaignPlan`. Fazal personally
reviews + approves these (Pillar 7 — owner-truth is the agent's structural
firewall).

The seam (VT-50, backlog) returns a verdict — `pass` or `revise` — with
optional per-category feedback. Two `revise` verdicts in a row → the draft
ships with `self_evaluate_status: failed_after_revisions`; the orchestrator
decides downstream.

## The four

### 1. Schema conformance

The draft is valid `CampaignPlan` JSON. Every required field per the v1.0
schema (`apps/team-orchestrator/src/orchestrator/agent/schemas/campaign_plan.py`)
is present and validates. No missing fields, no extra fields, no validator
violations. This is the structural check; the model can fail it by hand-rolling
JSON that drifts from the schema.

### 2. Pillar discipline

The agent's prose and numeric content must respect the four hardest pillars:

- **No invented numbers.** Footfall rates, churn baselines, persona traits,
  per-vertical heuristics from training data — all forbidden. Numbers come
  from the L4 skill corpus or `expected_arrr.basis` cites a real source.
- **No per-vertical heuristics.** "Cafés typically have a 30% return rate"
  is the canonical violation.
- **No overstated confidence.** ARRR is a range (`low_paise`, `high_paise`,
  `confidence` enum). Point estimates are a structural-schema violation
  AND a pillar violation.
- **No retention pressure.** "This is your last chance," "limited time only,"
  any high-pressure manipulation language in `message_plan.template_params`
  or `personalization`.

### 3. Internal consistency

The campaign's targeting matches its messaging. Specifically:

- `target_cohort.cohort_label` describes the customer-pattern the
  `message_plan.template_id` is intended for. A "dormant 90+ day" cohort
  paired with a "loyalty-thank-you" template is inconsistent.
- `expected_arrr` is plausible given `target_cohort.cohort_size`. A
  ₹1Cr `high_paise` projection for a 5-customer cohort is not.
- `escalation_conditions` are coherent — triggers reference actual fields,
  thresholds are realistic.
- `evidence_refs` cite real sources that support the prose claims
  (the schema already enforces marker-↔-claim_id two-way; this category
  catches semantically-weak citations the schema can't catch).

### 4. Legal compliance

No prohibited message content:

- High-pressure language (covered by Pillar discipline too — the overlap
  is intentional; legal is the harder rule).
- Misleading attribution claims ("guaranteed savings," "doubled sales").
- Financial-incentive claims that aren't backed by an actual offer
  registered with the tenant.
- Personal-data references the agent should not have access to (PII leak
  into `template_params`).

## Feedback shape (per category)

The seam returns one optional string per category — non-empty when that
category was the failing one. The gate renders these as a single user
message:

```
self_evaluate REVISE — address each:
- schema: missing expected_arrr.basis on proposed variant
- pillar: invented number in selection_reason ("60% of dormant customers...")
```

The agent receives this on the next turn; its revised draft re-enters the
gate.

## Bypass

The gate is structural (Pillar 8) — the agent cannot skip the call. If the
agent's transcript lacks a `self_evaluate` tool-use, the gate runs the seam
itself before permitting return. The tool-call counter (VT-35) increments
on every gate run, so the cap counts the gate's calls too.

## Config

`apps/team-orchestrator/config/self_evaluate.yaml` carries
`max_revisions: 2`. Raising the count is Type 2 governance.

## Evolution

The four categories are Type 2 commitments — adding a fifth or removing one
requires Clau + Fazal sign-off and a brief. Per-tenant criteria
customisation is explicitly Phase-2 scope; Phase 1 ships uniform criteria.
