---
title: "Long-term targeting: validate short-term surrogate signals before trusting noisy CLV predictions"
authored_by: codex_public_source_extraction
tags: [targeting, customer_lifetime_value, surrogates, retention, causal_inference]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: marketing_agent, finance_agent

Domains: marketing, finance

Source type: theory_simulation_and_field_experiment

Trust level: high

## Situation

A campaign decision must optimize long-term customer value, but the final outcome is delayed, sparse, and noisy while early engagement signals arrive quickly.

## Decision pressure

Teams either optimize the easy short-term metric as if it were value or fit heterogeneous effects directly to a noisy long-term outcome and obtain unstable targeting.

## Mistake or risk

Do not assume clicks, opens, first orders, or early frequency are valid surrogates. A treatment can improve the proxy while harming margin, habit quality, churn, or customer welfare.

## Recommended next action

Use completed historical cohorts to test whether early signals predict treatment effects on long-term contribution; separate frequency and churn mechanisms; cross-validate against simple policies; run a live holdout; monitor surrogate drift and long-term reversals.

## Evidence needed

- Completed cohorts with treatment, early signals, and long-term outcomes
- Contribution rather than gross activity
- Surrogate validity across segments and treatments
- Separate frequency, retention, and cost mechanisms
- Comparison with simple all/none rules
- Live holdout and delayed-outcome review

## Red flags / escalation triggers

- A correlational proxy is declared causal without validation
- Only short-term engagement is reported
- Model complexity beats no credible baseline
- Churn and frequency are collapsed into one opaque score
- Targeting continues after surrogate relationships drift

## Agent lesson

Marketing may use faster signals, but Finance validates that they preserve long-term contribution. The proxy earns trust only by predicting downstream treatment effects.

## Hard-gate candidate

Surrogate-based targeting requires completed-cohort validation, contribution outcomes, a simple-policy benchmark, live holdout, and delayed review.

## Retrieval triggers

- optimize CLV with short data
- marketing surrogate metric
- long term targeting
- coupon targeting retention
- noisy customer lifetime value

## Provenance

Source URL: https://www.hbs.edu/ris/download.aspx?name=23-023.pdf

Local source file: `archives/business-knowledge/research/hbs/23-023_5b02c937-1c15-42ea-9a95-ae7b6f17fd21.pdf`

Retrieved at: 2026-07-26
