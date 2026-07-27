---
title: "Quality at source: expose abnormalities immediately and stop harmful propagation"
authored_by: codex_public_source_extraction
tags: [jidoka, andon, quality_at_source, stop_the_line, frontline_authority]
priority: 4
version: 1
---

*Executional scenario — extracted from archived public sources for RAG grounding. Validate mutable law/platform policy before deterministic action.*

## Applies to

Agents: manager_coo

Domains: manager_coo

Source type: primary_operating_system_history

Trust level: high

## Situation

A defect, suspicious transaction, wrong message, compliance exception, bad data, or process abnormality appears early but can propagate through downstream work.

## Decision pressure

Stopping work reduces short-term throughput, so teams route the issue downstream for inspection, cleanup, reconciliation, or customer support.

## Mistake or risk

Do not optimize throughput by allowing known defects to continue. Downstream detection multiplies rework, customer harm, evidence loss, and root-cause distance.

## Recommended next action

Detect and visibly signal abnormality at the point of work; give the frontline actor authority to pause propagation within defined severity rules; contain affected work; identify and remove the cause; verify the countermeasure; resume and monitor.

## Evidence needed

- Abnormal condition and detection point
- Potential propagation and consequence
- Pause authority and severity threshold
- Containment scope
- Root cause and countermeasure
- Verification before restart

## Red flags / escalation triggers

- Operators fear punishment for stopping
- Known defects are queued for downstream cleanup
- Throughput target outweighs quality boundary
- Restart occurs before cause verification
- Alerts exist but no one can halt propagation

## Agent lesson

The Manager should design systems where bad work becomes visible and stoppable early. Quality is produced, not inspected in later.

## Hard-gate candidate

Material customer, money, data, or compliance abnormalities must fail closed or pause propagation until verified.

## Retrieval triggers

- stop the line
- jidoka
- known defect
- quality at source
- frontline pause

## Provenance

Source URL: https://www.toyota-global.com/company/history_of_toyota/75years/text/entering_the_automotive_business/chapter1/section4/item4.html

Local source file: `archives/business-knowledge/manager/decision-making/toyota_tps_jidoka.html`

Retrieved at: 2026-07-26
