---
title: "Pricing experiments: buy demand learning with a bounded loss budget"
authored_by: codex_public_source_extraction
tags: [pricing, experimentation, demand_learning, margin, governance]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: sales_agent, marketing_agent, finance_agent, manager_coo

Domains: sales, marketing, finance, manager_coo

Source type: field_experiment_and_pricing_algorithm

Trust level: high

## Situation

The business lacks reliable price-response evidence because products, inventory, segments, or seasons change and historical prices barely vary.

## Decision pressure

Teams either avoid learning to protect this month's revenue or change prices opportunistically without a design that can separate price response from product and time effects.

## Mistake or risk

Do not expose vulnerable customers to arbitrary discrimination, breach quoted or regulated prices, or allow an algorithm to explore without price, margin, volume, reputation, and cash limits.

## Recommended next action

Define the decision and reusable product attributes; estimate downside scenarios; preapprove a small exploration budget, price band, margin floor, customer-fairness rule, sample requirement, and stop conditions; randomize where feasible; then use the learning only in comparable contexts and continue monitoring.

## Evidence needed

- Price decision and comparable product attributes
- Baseline demand, margin, capacity, and seasonality
- Randomization or credible comparison design
- Maximum revenue/margin learning budget
- Customer fairness, contract, and legal constraints
- Demand response uncertainty and transfer test

## Red flags / escalation triggers

- Price changes coincide with promotion or assortment changes without controls
- No maximum downside or stopping rule
- Revenue is optimized while contribution or capacity is ignored
- Different customers discover unexplained unfair prices
- One experiment is applied to a materially different segment

## Agent lesson

Sales and Marketing frame willingness-to-pay hypotheses; Finance sets economic guardrails; the Manager approves the learning budget and fairness envelope, not individual prices.

## Hard-gate candidate

Adaptive pricing requires an approved learning budget, price and margin bounds, fairness review, causal design, and explicit transfer limits.

## Retrieval triggers

- how to test price
- pricing experiment
- unknown demand curve
- dynamic pricing SMB
- learn then earn

## Provenance

Source URL: https://www.hbs.edu/ris/download.aspx?name=Demand+Learning+and+Pricing+for+Varying+Assortments.pdf

Local source file: `archives/business-knowledge/research/hbs/Demand Learning and Pricing for Varying Assortments_d1e6413b-cfb4-4a7a-94d7-0d8534287f20.pdf`

Retrieved at: 2026-07-26
