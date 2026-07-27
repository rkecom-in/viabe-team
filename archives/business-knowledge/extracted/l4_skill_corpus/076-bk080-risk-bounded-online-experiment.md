---
title: "Online experiments: predefine harm limits and use valid sequential stopping rather than unsafe peeking"
authored_by: codex_public_source_extraction
tags: [ab_testing, experimentation, guardrails, sequential_testing, stop_condition]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: marketing_agent, sales_agent, manager_coo

Domains: marketing, sales, manager_coo

Source type: academic_working_paper

Trust level: high

## Situation

A campaign, offer, funnel, message, product, or pricing experiment may improve conversion but can also harm customers, revenue, trust, or compliance while it runs.

## Decision pressure

Teams either wait for a fixed sample while harm accumulates or repeatedly peek at ordinary significance tests and stop when the result looks favourable.

## Mistake or risk

Do not run a test without exposure and harm limits. Repeatedly checking a fixed-horizon p-value invalidates the claimed error rate; business urgency does not repair the statistics.

## Recommended next action

Write hypothesis, primary metric, guardrail/harm metrics, maximum exposure, minimum detectable effect, segment exclusions, and decision rule before launch; use a valid sequential method if early stopping is needed; preserve assignment and analyze both benefit and harm.

## Evidence needed

- Pre-registered hypothesis and causal decision
- Primary metric and minimum useful effect
- Guardrail and harm thresholds
- Maximum customer/revenue exposure
- Randomization and sample plan
- Valid fixed-horizon or sequential stopping rule

## Red flags / escalation triggers

- Test is stopped when p-value first crosses a threshold
- Only conversion is measured
- Vulnerable or regulated segments are included without review
- Sample ratio or assignment integrity is not checked
- Novelty or carryover effects are ignored

## Agent lesson

Specialists design the experiment; the Manager ensures downside is bounded and that the result can support the business decision claimed.

## Hard-gate candidate

Customer-facing experiments require predeclared harm limits, exposure caps, and a statistically valid stop rule.

## Retrieval triggers

- A/B test stop early
- experiment guardrails
- peeking p value
- campaign experiment
- conversion test harm

## Provenance

Source URL: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4472576

Local source file: `archives/business-knowledge/research/CONSULTING_ACADEMIC_RESEARCH_INDEX.md`

Retrieved at: 2026-07-26
