Before implementing any VT-* task, read its full page in Notion via MCP.
ViabeTeam_Sprint database ID: 20c8c0cc-7ba5-41cb-999e-77246cdefc51
Read the parent task's Expected Outcome + Notes first, then the subtask.
Architectural pillars are in the concept doc Section 5.
Recent decisions are in Clau_Session_Log; check Standing entries before assuming context.

Environment variables:
- Each app has its own .env.example (apps/*/.env.example). The root .env.example was removed.
- Frontend-bound vars: only in apps/team-web/.env.example; must be NEXT_PUBLIC_* prefixed.
- Server secrets: only in apps/team-orchestrator/.env.example or apps/team-ingestion-worker/.env.example; never NEXT_PUBLIC_*.
- The CI lint rule (scripts/lint-cross-product-env.mjs) enforces this separation.
- Local dev: copy each app's .env.example to .env.local in that app directory.
