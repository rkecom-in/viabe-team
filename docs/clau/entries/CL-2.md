---
cl_id: CL-2
entry_type: "Decision"
topic: "Deployment"
status: "Standing"
session_date: "2026-05-12"
created: "2026-05-12T12:12:30.740Z"
source: "Fazal 2026-05-12; chose path-based [viabe.ai/team](http://viabe.ai/team) routing + sibling repos + 2 production Postgres"
linked_tasks: "VT-120 (canonical), edits-overlay on VT-17, VT-2 parent doc, VT-21, VT-22, VT-23, VT-72, VT-78, VT-87, VT-11.1, VT-89"
notion_legacy_id: "35e387c2-cc5a-8161-821c-f8706e3db935"
title: "Deployment shape locked: sibling repos + shared accounts + separate projects within each account"
---

# CL-2 — Deployment shape locked: sibling repos + shared accounts + separate projects wit

Three locks: (1) Path-based [viabe.ai/team](http://viabe.ai/team) routing via thin viabe-router Vercel project that owns the apex and rewrites /team/* to viabe-team-web and /report/* to viabe-reports-web (Phase 1.5). Three Vercel projects total. (2) Sibling repos: rkecom-in/viabe-team (new) + rkecom-in/viabe-reports (existing, rename from 'viabe'). Shared utilities duplicated Phase 1; extraction to a third package = Phase 1.5+ only if drift becomes painful. (3) Two production Supabase projects (viabe-reports-prod + viabe-team-prod, both ap-south-1 Mumbai with ap-south-2 Hyderabad backup); cost trade-off accepted. Shared accounts: Vercel team, Railway org, Supabase org, Razorpay merchant (one KYC, separate plan IDs + webhook URLs + secrets), Twilio account (separate sender ID + DLT extension), Anthropic/OpenAI/Resend/Apify (separate API keys per env per product), LangSmith org (separate projects; Reports gets paid tier as side effect of Team's VT-114 upgrade). Razorpay webhook URL for Team: [viabe.ai/api/team/razorpay/webhook](http://viabe.ai/api/team/razorpay/webhook).

## Status history
- 2026-05-12T12:12:30.740Z: created (status=Standing)
- 2026-05-25 03:50 IST: migrated from Notion (notion_legacy_id: 35e387c2-cc5a-8161-821c-f8706e3db935)
