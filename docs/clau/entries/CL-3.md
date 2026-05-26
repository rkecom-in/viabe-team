---
cl_id: CL-3
entry_type: "Decision"
topic: "Privacy"
status: "Superseded"
session_date: "2026-05-12"
created: "2026-05-12T12:12:30.740Z"
source: "Fazal 2026-05-12; recorded as decision-of-record on VT-117 ADR-0003 via update_content edit"
linked_tasks: "VT-68, VT-74, VT-117 (ADR-0003)"
notion_legacy_id: "35e387c2-cc5a-81f2-bc28-d3f6bf7fe31a"
title: "K-anonymity threshold locked at k=5 for Phase 1; re-evaluation trigger is real SMB data, not tenant count"
---

# CL-3 — K-anonymity threshold locked at k=5 for Phase 1; re-evaluation trigger is real S

Chose k=5 over k=10 for Phase 1. Rationale: k=5 vs k=10 doesn't materially change privacy posture given Viabe's threat model (combined with locality coarsening + 180-day quarantine + business_type axis); k=5 maximizes probability that L3 patterns emit at all in Phase 1's small tenant base, which is the only way we can observe whether L3 contributes real value before raising the threshold. Re-evaluation trigger is observed evidence post-launch (whether patterns are emitting useful signal AND whether attackers could correlate aggregate L3 back to identifiable tenants), NOT a fixed tenant count. Raising k post-launch is always allowed (Type 3 commitment is no lowering below published value). Phase 1 reality: with 10 design partners + ~5 business types + ~15-20 city tiers, most cohorts will fail to hit k=5; L3 likely empty Phase 1 regardless of k. Quarantine + empty-pattern fallback path tested.

## Status history
- 2026-05-12T12:12:30.740Z: created (status=Superseded)
- 2026-05-25 03:50 IST: migrated from Notion (notion_legacy_id: 35e387c2-cc5a-81f2-bc28-d3f6bf7fe31a)
