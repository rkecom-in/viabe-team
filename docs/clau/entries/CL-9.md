---
cl_id: CL-9
entry_type: "Question for Fazal"
topic: "Knowledge Architecture"
status: "Open"
session_date: "2026-05-12"
created: "2026-05-12T12:13:25.780Z"
source: "Clau flagged during batch completion 2026-05-12; remains unresolved"
linked_tasks: "VT-70, VT-71"
notion_legacy_id: "35e387c2-cc5a-81a2-96b9-dc7504598d7a"
title: "VT-70 retrieve_l4_skills MCP tool: drop or keep? Clau recommends drop; Fazal hasn't confirmed"
---

# CL-9 — VT-70 retrieve_l4_skills MCP tool: drop or keep? Clau recommends drop; Fazal has

VT-70 was authored to add `retrieve_l4_skills` as a 12th MCP tool that the agent can call to fetch L4 skill documents. Clau recommended dropping it: composition (VT-71) handles L4 implicitly via the composition layer that merges L1-L4 into the agent's context bundle; an explicit retrieval tool is redundant and adds a way for the agent to over-fetch L4 documents (token cost). Fazal has not confirmed the drop. Default recommendation if no confirmation: drop the tool, let VT-71 composition handle L4 implicitly. This decision needed before Sprint 7 starts.

## Status history
- 2026-05-12T12:13:25.780Z: created (status=Open)
- 2026-05-25 03:50 IST: migrated from Notion (notion_legacy_id: 35e387c2-cc5a-81a2-96b9-dc7504598d7a)
