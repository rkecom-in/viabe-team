---
title: "AI deployment: map and test the capability frontier at task level before trusting polished output"
authored_by: codex_public_source_extraction
tags: [generative_ai, human_ai, task_design, quality, verification]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: manager_coo

Domains: manager_coo

Source type: peer_reviewed_preregistered_field_experiment

Trust level: high

## Situation

A business wants agents or employees to use generative AI across a knowledge workflow containing apparently similar research, analysis, writing, and judgment tasks.

## Decision pressure

Strong speed and quality on many tasks creates generalized trust, while failures outside the model's capability frontier can remain fluent and difficult to detect.

## Mistake or risk

Do not infer workflow-wide reliability from average gains. In the preregistered experiment with 758 consultants, AI improved completion, speed, and quality on in-frontier tasks but users were 19% less likely to solve the selected outside-frontier managerial task correctly.

## Recommended next action

Decompose workflows into decisions and subtasks; create representative gold cases including adversarial boundary cases; compare human, AI, and human-plus-AI performance; require source/evidence checks and human ownership for uncertain or consequential tasks; re-evaluate after model or workflow changes.

## Evidence needed

- Task inventory and consequence classification
- Representative gold-standard cases
- Human, AI, and combined accuracy/quality/time
- Boundary and deceptive-failure examples
- Verification method and accountable human
- Model/version and re-evaluation trigger

## Red flags / escalation triggers

- One benchmark score authorizes an entire workflow
- Fluency is treated as correctness
- Employees cannot identify when independent verification is required
- High-consequence decisions lack source checks
- Model upgrades silently change behavior

## Agent lesson

The COO governs AI as a changing task-level capability map, not a binary adoption decision. Strong performance in one step can increase overreliance in the next.

## Hard-gate candidate

Consequential AI-assisted workflows require task-level evals, boundary cases, accountable verification, and version-triggered revalidation.

## Retrieval triggers

- where to use AI
- AI hallucination business
- human in the loop
- AI workflow evaluation
- jagged frontier

## Provenance

Source URL: https://doi.org/10.1287/orsc.2025.21838

Local source file: `archives/business-knowledge/research/management/hbs_jagged_ai_frontier.pdf`

Retrieved at: 2026-07-26
