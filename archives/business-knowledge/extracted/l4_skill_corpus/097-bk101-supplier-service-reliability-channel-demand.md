---
title: "Channel sales: treat fill rate and service reliability as demand-generation variables"
authored_by: codex_public_source_extraction
tags: [channel_sales, fill_rate, supplier_service, retailer_demand, reliability]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: sales_agent, finance_agent, manager_coo

Domains: sales, finance, manager_coo

Source type: longitudinal_supplier_retailer_data_and_pilot

Trust level: medium

## Situation

Retailers, distributors, or resellers reduce orders even though list price, sell-through opportunity, and seller activity appear competitive.

## Decision pressure

Sales reaches for discounts and incentives because service failures, partial fills, substitutions, claims, and recovery delays are recorded in operations rather than the account plan.

## Mistake or risk

Do not copy the apparel study's estimated service-demand relationship as a universal elasticity or assume correlation proves every order decline was caused by fill rate.

## Recommended next action

Join account orders with requested versus supplied quantity/date, stockouts, substitutions, claims, recovery time, sell-through, and margin; identify reliability-sensitive accounts/SKUs; repair root causes with Operations before funding a price concession; test whether restored service changes future orders.

## Evidence needed

- Requested and fulfilled quantity/date by account and SKU
- Fill rate, stockouts, substitutions, and recovery time
- Retailer sell-through and inventory position
- Order trend before and after service changes
- Price, promotion, assortment, and season controls
- Contribution impact of service fix versus discount

## Red flags / escalation triggers

- Discount is offered before service history is reviewed
- Aggregate fill rate hides priority-account failures
- Revenue credit ignores partial or late fulfilment
- Seller cannot see claims and stockouts
- Pilot result is presented as guaranteed causal uplift

## Agent lesson

Sales diagnoses the account mechanism, Finance compares service-fix and discount economics, and the Manager resolves cross-functional ownership.

## Hard-gate candidate

Material channel discounts require account-level service reliability, sell-through, order trend, and contribution evidence.

## Retrieval triggers

- distributor orders declining
- retailer not reordering
- fill rate sales impact
- channel service level
- discount or improve delivery

## Provenance

Source URL: https://www.hbs.edu/ris/download.aspx?name=11-034.pdf

Local source file: `archives/business-knowledge/research/hbs/11-034_95f981a4-388b-40f3-9751-fb654b05162e.pdf`

Retrieved at: 2026-07-26
