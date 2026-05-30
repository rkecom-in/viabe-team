# Latest State Snapshot

**As of:** 2026-05-29 22:45 IST (Cowork-authored, end of marathon session).
**Main HEAD:** `d8a3e20` (PR #134 VT-237 password auth). **Branch tip:** `d6ba63d` (VT-40 fix-1, unmerged). **Reports-Jun15 in 17 days.**

---

## CRITICAL PATH

Sprint 2 substrate is shipped + Ops Console hot-fixes are done. The day cleared the Sprint 2 Integration Agent epic + Sprint 2 Cost+Moat (the parts non-blocked by Razorpay) + Sprint 9 docs cluster + 4 hot-fixes from Fazal's manual walks through the dev env (VT-230 /login redirect, VT-232 login styling, VT-233 Supabase fragment, VT-235 Ops Console styling, VT-236 7-day session, VT-237 password auth). Auth works end-to-end: magic-link OR password → 7-day operator JWT cookie → Ops Console pages styled + functional. Customer-data substrate is consent-gated + RLS-isolated. Next critical-path cluster: terminal-style stream view (VT-238, queued + dispatched but CC unresponsive), 4 SR-Agent MCP tools (VT-40 in-flight, VT-41/42/46 not started), and the dynamic operator allowlist (VT-228 deferred until VT-237 verified). After those: Sprint 8 launch cluster, mostly Fazal/vendor blocked.

## IN FLIGHT

**48 merges shipped today.** Final 5 unfinished items handed off for tomorrow's Opus 4.8 session: VT-40 PR #135 (query_customer_ledger MCP tool, fix-1 on branch awaiting CI green/auto-merge — resume with `gh pr checks 135 --watch`); VT-41/VT-42/VT-46 (get_business_profile + get_recent_campaigns + match_transactions MCP tools, not started; standalone callable like VT-49 pattern; brief intact at `.running/to-claudecode/20260529T215000Z-brief-ready-batch10-VT-237-40-41-42-46.md`); VT-238 (terminal-style stream view redesign, brief dispatched but not picked up; brief at `.running/to-claudecode/20260529T223000Z-brief-ready-VT-238-terminal-stream.md`; visual layer replacement only — VT-201 Realtime substrate preserved). CC orchestrator stopped responding ~22:25 IST after VT-40 fix-1; direct status-check signal sent 22:40 IST got no reply. Likely orchestrator daemon crash, paused, or context budget hit. Tomorrow's session should `ps aux | grep cc-orchestrator` first thing — restart if needed.

## BLOCKED ON

VT-225 design doc review (Fazal-side, low priority — L0 per-tenant k-anonymity admission doc shipped via PR #128; awaits Fazal review). VT-228 dynamic operator allowlist (auth-surface coordination — touches same files as VT-237/VT-233/VT-236; deferred to ship after VT-237 is verified working). VT-231 prod Mumbai provisioning (Fazal-side, launch-blocker — provision viabe-team-prod Supabase in ap-south-1 with all migrations + secrets rotation; hard constraint per CL-422: no real customer data on dev until VT-231 closes). Sprint 8 launch cluster (Fazal/vendor blocked — Razorpay Live KYC, Twilio DLT, landing page copy). VT-108/109/111/113/114/115 vendor approvals (Fazal-side — Meta templates, Razorpay Live, Twilio DLT, Apify, Resend DMARC, LangSmith billing, DPDP final review).

## NEXT ACTION

Tomorrow's Opus 4.8 Cowork session: (1) read this snapshot + `docs/clau/decisions-ledger.md` first; (2) check git log + `ls .running/to-cowork` for any CC signals arrived overnight; (3) verify VT-237 env vars are set in Vercel (`OPERATOR_EMAIL` + `OPERATOR_PASSWORD`) and confirm Fazal can log in with password; (4) resume VT-40 PR #135 (re-run CI or merge if green); (5) re-dispatch VT-41/42/46 + VT-238 if CC is alive again; otherwise file as "next session" backlog; (6) after in-flight queue closes, propose batch 11 — VT-228 dynamic operator allowlist + remaining SR-Agent tools (VT-43/44/45/47/48 with appropriate substrate gates) + any Fazal-surfaced product feedback from overnight testing.

## DO NOT

Do NOT re-litigate Standing decisions in `docs/clau/decisions-ledger.md`. Three new entries today: **CL-421** (zero-paste connectors, Locked Standing), **CL-422** (Seoul dev DB accepted, Standing-with-sunset). Do NOT re-architect VT-237 env-password auth — Fazal explicitly chose env-var-stored password over Supabase Auth password. When VT-228 multi-operator allowlist ships, the env-password approach migrates to per-operator hashes in the allowlist table; documented in VT-237 brief as Phase-2 migration note. Do NOT trigger magic-link emails during testing — VT-237 password path is the operator's primary login now. Do NOT roll back VT-235 Ops Console card styling for the terminal redesign — VT-238 ONLY replaces `/team/ops/stream` (live stream). Workspace + history + per-run + per-tenant pages keep gray-50/card aesthetic.

---

## What changed since 2026-05-25 ~23:40 IST (prior snapshot)

| Change | Where to read more |
|---|---|
| **48 merges in one session** — Sprint 2 Integration Agent epic + Sprint 2 Cost+Moat partial + Sprint 9 docs cluster + 4 Ops Console hot-fixes + VT-237 password auth | `git log --oneline -50` |
| CL-421 filed — all integration-agent connectors zero-manual-paste after OAuth | `decisions-ledger.md` |
| CL-422 filed — dev DB Seoul-accepted (free-tier); prod = Mumbai (VT-231 launch-blocker) | `decisions-ledger.md` |
| 5 follow-up rows from batch 8 — VT-225 (L0 k-anon design doc, shipped), VT-226 (webhook_metrics async, shipped), VT-227 (twilio replay purge, shipped), VT-228 (allowlist table, queued), VT-229 (region canary, cancelled per CL-422), VT-231 (prod Mumbai, queued) | `.viabe/sprint/VT-22*` |
| Ops Console end-to-end working — magic-link → fragment-finalize → operator JWT → styled UI; password login at `/team/ops/login` once Vercel env vars set | `apps/team-web/app/(auth)/team/ops/login/` |
| Cookie path + TTL hardened — path=/team, 7-day TTL, FAZAL_OWNER_UUID allowlist gate, constant-time password compare | `apps/team-web/app/api/ops/login/route.ts` |
| GitHub Actions billing block hit + resolved — Fazal raised on-demand budget | (no code change) |
| Memory updates: dev-DB Seoul accepted, self-triggered CC poll | `~/Library/.../memory/*` |

## How to read this snapshot

Per Clau's operating brief §4 item 1, this is the FIRST file a fresh session reads. Then `docs/clau/decisions-ledger.md`. Then if something is unclear, the relevant `docs/clau/entries/CL-<N>.md`. Tomorrow's session: status check CC first, then resume the deferred queue.
