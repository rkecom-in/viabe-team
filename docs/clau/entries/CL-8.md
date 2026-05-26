---
cl_id: CL-8
entry_type: "Tech Debt"
topic: "Architecture"
status: "Open"
session_date: "2026-05-12"
created: "2026-05-12T12:13:25.780Z"
source: "Clau noticed during batch completion 2026-05-12; flagged in compaction memory; never edited into the actual VT-4 page"
linked_tasks: "VT-4 parent doc, VT-32 (agent SDK skeleton), VT-103, VT-105"
notion_legacy_id: "35e387c2-cc5a-8114-8589-d630042b69f4"
title: "VT-4.4 hard-limits enforcement missing the ₹50 per-run cost ceiling axis (4 axes documented, 5 needed)"
---

# CL-8 — VT-4.4 hard-limits enforcement missing the ₹50 per-run cost ceiling axis (4 axes

VT-4 (Sales Recovery Agent) parent doc says 'hard limits: 80K tokens, 25 tool calls, depth 8, 5min wall clock' across 4 axes. VT-103 (cost dashboard) + VT-105 (hard-limits instrumentation) both reference a 5th axis: ₹50 per-run cost ceiling. This 5th axis was added to the spec AFTER VT-4 was authored. Implementation team needs to know VT-4.4 has a missing dimension. Surface this on VT-4.4 PR pickup; don't let CoderC ship VT-4.4 with only 4 axes.

## Status history
- 2026-05-12T12:13:25.780Z: created (status=Open)
- 2026-05-25 03:50 IST: migrated from Notion (notion_legacy_id: 35e387c2-cc5a-8114-8589-d630042b69f4)
