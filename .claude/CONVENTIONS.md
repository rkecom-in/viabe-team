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
- Pillar 8 naming: every Viabe Team backend secret uses the TEAM_* prefix. No exceptions.
- Exception (VT-3.3b): Next.js route handlers run server-side and may need server-only
  vars. apps/team-web/.env.example whitelists TEAM_TWILIO_AUTH_TOKEN + INTERNAL_API_SECRET
  by exact name (WEB_ENV_WHITELIST in the lint rule). Any other non-NEXT_PUBLIC_
  secret-suffixed var in the web env is still rejected.

Merge ritual (Clau_Session_Log CL-87):
- Fazal updates the Notion subtask Status to "Done" BEFORE clicking merge.
- Merge command: `gh pr merge <N> --squash --delete-branch --admin` (solo-dev repo: --admin
  bypasses self-approve restriction; legitimate Phase 1 trade-off).
- Post-merge: `git checkout main && git pull && git branch -d <merged-branch>` to keep
  Claude Code's local tree current.
- Claude Code does NOT attempt Notion Status updates post-merge — Fazal-side ritual.

Env-rename PR ritual (Clau_Session_Log CL-93):
- Never atomic-swap an env var name. Always pre-merge double-set.
- Procedure: (1) Set both OLD_NAME and NEW_NAME in the deploy environment (Railway/Vercel)
  with the same value, (2) merge the PR, (3) confirm renamed code path is healthy in
  production logs, (4) delete OLD_NAME from the deploy environment.
- Claude Code flags env-rename PRs in the PR body: "Requires pre-merge double-set per CL-93".

Stacked PR convention (Clau_Session_Log CL-88):
- When a fix-PR has a real semantic dependency on another open PR (edits the same function
  in the same way), stack on the open PR's branch rather than branching off main.
- PR base in GitHub = the open PR's branch. After upstream merges, GitHub auto-retargets
  the stacked PR's base to main; one-click rebase, no manual intervention.
- Stacking is acceptable for 2-deep. At 3+ deep, prefer waiting for upstream merge.
- Document dependency in the PR description: "Stacked on #N; merge order: #N then this".

Discipline rules (Clau-side, from Resurrection File v2.18):
- Re-read Notion subtask Out-of-scope section line-by-line before drafting any brief.
- Context7 docs occasionally lag; live PyPI/GitHub source wins on disagreement.
- psycopg dict_row factory: column-name access only, never positional.
- Cross-app brief verification BEFORE drafting: env vars, dependencies, contracts, Pillar 8.
- Pre-brief audit-blocker check: search Clau_Session_Log for Open Next Action / Question /
  Blocker entries before drafting code-shipping briefs.
- Audit-session Decision Authority self-enforcement: for findings in Clau-owned categories
  (architecture, schema, code design, CI config), produce DECISIONS not QUESTIONS.
- Pre-delivery brief consistency scan: acceptance line-by-line against implementation;
  caveats must update both sections.
