# Latest State Snapshot

**As of:** 2026-05-25 ~23:40 IST (Cowork-authored, supersedes CL-407 / 2026-05-24 close).
**Main HEAD:** `449a98b` (PR #56 VT-101 LangSmith). Reports-Jun15 in **21 days**.

---

## CRITICAL PATH

**Sprint 1 Foundation closure → Sprint 8 Owner Surface/Billing.** Sprint 1 has 14 Backlog leaves remaining; **VT-102 (pipeline_log structured event store)** is the next leaf by Exec Order (1 of 14 left after VT-101 merged). Sprint 1 must wrap before Reports-Jun15 because VT-103/104/121 (cost dashboard, PII redactor, Telegram launch tracker) all depend on VT-102, AND Sprint 8 (Razorpay Live, landing page, sign-up, founding-tier counter — 30 mostly-Backlog items) is the actual launch-blocker cluster for Jun15. Don't confuse "ship-thin owner_inputs verification done" (which closed today via PR #53 VT-OIV/VT-155) with "Reports-Jun15 done."

## IN FLIGHT

Nothing executing. PR #56 (VT-101) merged at 23:35 IST — first task to ship under the post-Notion operating model + Rule #15 canary discipline. Five PRs shipped overnight today: #52 (VT-4 ship-thin, earlier today), #53 (VT-OIV / VT-155 owner_inputs verification), #54 (VT-166 agent-loop daemon), #55 (VT-168 CI workflow self-heal), #56 (VT-101 LangSmith + run_id propagation). Sprint 1 active queue count is currently **0**. Interactive `claude -c` watch loop remains primary; agent-loop daemon installed and paused via STOP file.

## BLOCKED ON

Nothing actively blocked from CC progress. Two Fazal-side decisions are open and modest in urgency: (1) **VT-164** per-tenant attribution wiring — Fazal needs to confirm-or-revise the `target_recovered_paise = max(last_7d_recovered_paise × 1.1, ₹500)` product rule before that brief can be implemented (rule lives in serializer constant today as ship-thin scaffolding). (2) **VT-156** privacy notice — Fazal owns drafting + lawyer review + sign-off; current ship-thin posture is "privacy notice not yet drafted" but it's a launch-gate per CL-389/CL-390 locked privacy decisions (CL-385).

## NEXT ACTION

**Queue VT-102 (pipeline_log)** with a Rule #15 Canary section added to its sprint file before brief-ready signal goes out. Migrated `.viabe/sprint/VT-N.md` files pre-date Rule #15 — until I do a bulk pass, every brief I queue from now needs a Canary section appended at queue-time. CC writes the canary alongside implementation; pre-merge-check just runs it. After VT-102 ships, sequence continues per Exec Order: VT-103 → VT-104 → VT-28 → VT-121 → VT-30 → ... Sprint 1 estimated 5-7 more PRs to wrap, several days of CC time.

## DO NOT

Do NOT queue any brief without a Canary section (Rule #15, Fazal-Standing 2026-05-25). Do NOT sort within-sprint by Priority before Exec Order — Exec Order is the planned sequence; Priority is the tier within (Rule per `feedback_exec_order_first.md`). Do NOT write to Notion — read-only archive since 2026-05-25 ~04:30 IST migration. Do NOT auto-merge any PR — Pillar 7 requires Fazal's explicit `type: task` with `authorized_by: fazal` for every merge. Do NOT trust per-conversation auto-memory across Dispatch / fresh windows / phone — those are separate spaces; cross-space context lives in `docs/clau/` + `CLAUDE.md` + the Cowork Project's instructions field. Do NOT re-litigate Standing decisions in `docs/clau/decisions-ledger.md` — particularly the privacy locks (CL-385/CL-389/CL-390), the memory architecture (CL-324: L1 hand-built / L2-L3 Mem0-deferred), CampaignPlan v1.0 (CL-260), and the four-role operating model (`docs/clau/operating-brief.md`).

---

## What changed since CL-407 (2026-05-24 close)

| Change | When | Where to read more |
|---|---|---|
| 5 PRs merged (#52..#56) | 2026-05-24 → 25 | `git log origin/main --oneline -10` |
| Notion → repo migration (167 sprint files + 369 CL entries + 4 consolidated docs) | 2026-05-25 ~04:30 IST | `.viabe/sprint/README.md`, `docs/clau/operating-brief.md` §3 |
| Clau operating brief v2 (four-role model, Cowork autonomous on sequencing) | 2026-05-25 ~03:15 IST | `docs/clau/operating-brief.md` |
| Discipline Rule #15 (canary mandatory) | 2026-05-25 ~20:50 IST | `docs/clau/discipline-rules.md` §Rule #15 |
| Cowork Project + default instructions configured for Dispatch routing | 2026-05-25 ~13:30 IST | `feedback_cowork_projects.md` memory |
| Dashboard regenerator + sprint-wise filter | 2026-05-25 ~04:35 IST | `scripts/build_dashboard.py` + scheduled task `viabe-team-dashboard-regen` (every 10 min) |
| VT-Foundation status audit (6 stale rows → Done) | 2026-05-25 ~02:30 IST | `docs/clau/entries/` audit log entries `36a387c2…81dc` + `…81ab` |
| LangSmith dev workspace + Service Key in `.viabe/secrets/langsmith-dev.env` | 2026-05-25 ~23:00 IST | secrets/ README.md inventory |

## How to read this snapshot

Per Clau's operating brief §4 item 1, this is the FIRST file a fresh session reads. Then `docs/clau/decisions-ledger.md`. Then if something is unclear, the relevant entry in `docs/clau/entries/CL-<N>.md`.

The 5 fields are deliberate; don't add a sixth. If you need to capture more state, add a "What changed since…" section underneath (as here) — not new top-level fields.

Snapshot regenerated at session-end going forward. The prior CL-407 long-form entry remains in `docs/clau/entries/CL-407.md`.
