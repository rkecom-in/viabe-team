---
title: "Promotion targeting: learn incrementality from a consistent experiment library"
authored_by: codex_public_source_extraction
tags: [promotions, incrementality, experimentation, targeting, machine_learning]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: marketing_agent, finance_agent, sales_agent

Domains: marketing, finance, sales

Source type: large_multi_experiment_machine_learning_study

Trust level: high

## Situation

The business has run many randomized offers and wants to decide which customers should receive a new promotion without discounting customers who would buy anyway.

## Decision pressure

Response propensity is easier to predict than causal lift, so high-propensity customers receive margin giveaways even when the promotion did not change their behaviour.

## Mistake or risk

Do not deploy an incrementality model when experiment definitions, outcomes, eligibility, or randomization are inconsistent. Historical patterns can fail after a new channel, product, segment, or offer concept.

## Recommended next action

Create an experiment registry with treatment, control, eligibility, exposure, cost, outcome window, and contribution; train only on comparable randomized history; reserve a holdout for the new campaign; compare model targeting with simple policies; monitor uplift, margin, retention, complaints, and drift.

## Evidence needed

- Randomized experiment registry and stable definitions
- Treatment exposure and control integrity
- Incremental orders, contribution, and retention
- Offer/customer/channel similarity to history
- Out-of-sample and new-campaign holdout performance
- Privacy, fairness, and exclusion rules

## Red flags / escalation triggers

- Only redeemers or attributed buyers are analyzed
- No untreated control exists
- Discount cost is excluded from uplift
- Outcome windows changed across experiments
- A materially new offer is launched without a fresh holdout

## Agent lesson

Marketing owns causal targeting design, Finance owns incremental contribution, and Sales checks channel effects. A response model is not an incrementality model.

## Hard-gate candidate

Model-targeted promotions require randomized historical evidence, stable definitions, a live holdout, contribution economics, and drift monitoring.

## Retrieval triggers

- promotion incrementality
- discount customers who would buy
- uplift model
- coupon targeting
- causal marketing

## Provenance

Source URL: https://www.hbs.edu/ris/download.aspx?name=24-076.pdf

Local source file: `archives/business-knowledge/research/hbs/24-076_c3424b9b-adbb-4aa9-897f-c29ba27687aa.pdf`

Retrieved at: 2026-07-26
