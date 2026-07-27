---
title: "Indian SMB benchmarking: match the survey universe and weights before using a statistic"
authored_by: codex_public_source_extraction
tags: [india, smb, benchmarking, world_bank, survey_weights]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: manager_coo, finance_agent, marketing_agent, sales_agent

Domains: manager_coo, finance, marketing, sales

Source type: official_weighted_business_survey_and_metadata

Trust level: high

## Situation

An agent uses an Indian business statistic to set a target, diagnose a client, size a problem, compare finance access, or justify a sales/marketing recommendation.

## Decision pressure

A credible publisher and a precise percentage make the number look universal even when the sample covers a specific firm size, registration status, city set, sector, period, and weighted survey design.

## Mistake or risk

Do not treat raw case counts as population prevalence or combine the Micro Enterprise Survey with the broader Enterprise Survey as if their universes and questionnaires were identical.

## Recommended next action

Record the exact variable/question, universe, fieldwork and fiscal period, firm-size and registration definition, cities/regions, sector, missing-value codes, stratification, and weight; calculate weighted estimates with uncertainty where microdata access permits; state comparability gaps; use the benchmark as context, not a diagnosis.

## Evidence needed

- Exact questionnaire item and response coding
- Target population, registration and employee-size definition
- Cities/regions, sector coverage, and reference period
- Sampling strata, weights, missing values, and uncertainty
- Client metric definition and comparability gaps
- Microdata access/licence or published table provenance

## Red flags / escalation triggers

- Sample percentage is reported without applying weights
- Nine-city micro-firm result is called all-India SMB prevalence
- Different fiscal periods or variable definitions are compared
- Correlation is presented as a recommended causal intervention
- Restricted or registered microdata is obtained outside its licence process

## Agent lesson

Specialists own domain interpretation; the Manager asks whether the comparison population, definition, period, and decision mechanism actually match the business.

## Hard-gate candidate

Business benchmarks used in recommendations require variable provenance, matched universe and period, survey weights where applicable, uncertainty, and explicit comparability limits.

## Retrieval triggers

- India small business benchmark
- World Bank enterprise survey India
- micro enterprise statistics
- compare SMB performance
- survey weights business

## Provenance

Source URL: https://microdata.worldbank.org/catalog/6495

Local source file: `archives/business-knowledge/research/datasets/ddi-documentation-english_microdata-6495.pdf`

Retrieved at: 2026-07-26
