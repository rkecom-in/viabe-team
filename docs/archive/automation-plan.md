> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# Viabe Team — Automation Architecture v1.0

**Status:** Locked 2026-05-24 by Fazal. v1.0 — iterate as we learn. This document is the operating contract between Fazal (CEO), Cowork (delivery captain), Clau (architect-on-call), and Claude Code (developer).

## 1. Roles and escalation

| Agent | Role | Always does | Never does |
|---|---|---|---|
| **Fazal (CEO)** | Authority, gate, signer | Type-3 decisions (model picks, pricing, vendor terms, scope), PR merge button, lawyer engagement, final sign-off on Standing decisions | Daily relay of briefs between Cowork/Clau/Claude Code (eliminated), answering Claude Code's clarifications, manual brief drafting |
| **Cowork (delivery captain)** | PM + auditor + brief drafter | Task ordering, brief drafting for routine work, plan review on Claude Code submissions, audit + drift catching, queue management, ALIGNMENT ACK on every Clau snapshot, dashboard + Daily Brief | Merge PRs, trigger Claude Code remotely, sign anything, skip Clau on architectural calls, surface settled items to Fazal |
| **Claude Code (developer)** | Implementation | Read briefs from queue, write plan, await Cowork review, implement on approval, run tests, open PR, append every decision to `task_log.md`, surface clarifications to Cowork (not Fazal) | Auto-merge, escalate routine questions to Fazal, exceed budget cap silently, ship without Cowork plan-review |
| **Clau (architect-on-call)** | Deep architectural deliberation | New external services/deps, schema migrations, privacy/security posture shifts (CL-385 changes), multi-module tradeoffs, anything that creates a new Standing decision, session-log narrative + snapshots | Daily brief drafting (now Cowork's job), relay-via-Fazal for routine work |

**Escalation paths:**

```
Claude Code → Cowork           (routine clarifications; via .running/to-cowork/)
Cowork → Clau                  (architectural questions; via Fazal paste into Claude Chat)
Cowork → Fazal                 (Type-3 decisions, merge requests, Telegram for time-sensitive)
Clau → Cowork                  (new snapshots; via Clau_Session_Log; Cowork ACKs)
```

**Default-to-escalation rule:** if Cowork is uncertain whether a question is routine or architectural, escalate to Clau. Better one extra Clau session than a quietly bad decision.

## 2. The task queue — `.viabe/queue/`

Per-task directory, git-tracked, branch-protected.

```
.viabe/queue/
├── VT-101/                           # one directory per task, VT-ID is the key
│   ├── status                        # single line: queued | planning | review | implementing | in-pr | merged | done | blocked
│   ├── brief.md                      # Cowork-drafted brief; reads from VT-row Expected Outcome + project context
│   ├── plan.md                       # Claude-Code-written implementation plan
│   ├── review.md                     # Cowork-written plan review: APPROVED or REVISIONS
│   ├── task_log.md                   # Claude-Code-written append-only log of every decision made
│   ├── pr.md                         # PR URL + summary when opened
│   └── clau-needed.md                # OPTIONAL — Cowork files if it needs Clau on something
├── done/                             # completed task dirs move here after merge
└── README.md                         # queue protocol spec (links to this doc)
```

**Status state machine:**

```
queued → planning → review → implementing → in-pr → merged → done
   │         ↑         │           ↑
   │         └─────────┘           │
   │      (revisions requested)    │
   └──────────────────────────────→ blocked
                                    (any state can become blocked;
                                     blocked items appear in Daily Brief
                                     until unblocked or deferred)
```

State transitions:
- `queued`: Cowork wrote the brief; awaiting Claude Code pickup.
- `planning`: Claude Code is writing `plan.md`.
- `review`: Plan written; Cowork reviewing.
- `implementing`: Plan approved; Claude Code executing.
- `in-pr`: PR opened; CI running.
- `merged`: Fazal merged.
- `done`: Cowork has verified post-merge artifacts (Notion updates, doc-update PRs filed if needed). Directory moves to `.viabe/queue/done/`.

**Who writes what to status:** any agent who's taking action writes the new status. The state machine is enforced by convention, not code (for now).

## 3. Communication layer — `.running/`

**Gitignored ephemeral signaling.** Timestamped per-message files, no race conditions, full history via `processed/`.

```
.running/
├── to-cowork/                                       # Claude Code writes here, Cowork reads
│   ├── 2026-05-24T15-30-00-q-VT-101.md             # "question on VT-101 brief"
│   ├── 2026-05-24T15-45-00-plan-ready-VT-101.md    # "plan.md written, ready for review"
│   └── 2026-05-24T16-30-00-done-VT-101.md          # "task complete, PR #53 open"
├── to-claudecode/                                   # Cowork writes here, Claude Code reads
│   ├── 2026-05-24T15-32-00-a-VT-101.md             # "answer to your question on VT-101"
│   ├── 2026-05-24T15-50-00-review-VT-101.md        # "plan APPROVED" or "REVISIONS"
│   └── 2026-05-24T17-00-00-brief-VT-102.md         # "next brief queued, see .viabe/queue/VT-102/brief.md"
├── processed/                                        # both sides move files here after acting
│   └── ...
└── README.md                                         # protocol spec (gitignored content)
```

**Message format** (single file, markdown):

```markdown
---
from: claudecode
to: cowork
task: VT-101
type: question | answer | plan-ready | review | brief-ready | done | blocked
ts: 2026-05-24T15:30:00+05:30
---

Body of the message — markdown. Be specific. Reference file paths and line numbers
when relevant. If asking a question, state what you've already tried.
```

**Read semantics:** the receiver scans their inbox dir, processes oldest first, moves processed file to `.running/processed/`. Atomic file moves; no race condition. If two messages arrive at the same timestamp (unlikely), filename collision is resolved by adding a sequence suffix.

**No empty-after-read.** History preserved in `processed/`.

## 4. Notification channels — when to use what

| Channel | When | Who reaches whom |
|---|---|---|
| **Cowork chat (this session)** | Real-time discussion while Fazal is active | Fazal ↔ Cowork |
| **Notion `Clau_Session_Log`** | Persistent state, decisions, corrections, snapshots, alignment acks | All four parties; durable record |
| **`.viabe/queue/` files** | Per-task state — briefs, plans, reviews, task logs | Cowork ↔ Claude Code |
| **`.running/` files** | Ephemeral signaling between Cowork and Claude Code | Cowork ↔ Claude Code |
| **Notion `ViabeTeam_Sprint`** | Authoritative VT-ID rows (today); migrating to GitHub Projects (Section 6) | Read-mostly |
| **Telegram (planned, requires daemon)** | Time-sensitive async push to Fazal when not in Cowork: PR ready to merge, CI green, blocker needing Type-3 | Cowork → Fazal |
| **GitHub PR comments** | Code-specific discussion attached to a diff | Cowork ↔ Claude Code, also Clau when invited |
| **Email** | Not used | — |

**Telegram activation** — see Section 8 for the daemon build. Until then, time-sensitive items go to Cowork chat with a note "would have been Telegram."

## 5. Daily cycle — typical task walk-through (owner_inputs verification as the example)

**T+0:00 — Brief drafted (Cowork in chat or scheduled task)**
- Cowork reads VT row Expected Outcome + project context + Step-0 result.
- Writes `.viabe/queue/VT-XXX/brief.md`, sets `status: queued`.
- Writes `.running/to-claudecode/<ts>-brief-VT-XXX.md` signaling "new brief available."

**T+0:01 — Claude Code picks up (manual today; daemon in Phase 2)**
- Fazal kicks off Claude Code in Max effort mode, pointed at the brief.
- Or: launchd daemon (Section 8) watches `.viabe/queue/*/status` for `queued`; fires Claude Code on file event.

**T+0:05 — Claude Code plans**
- Reads brief, project context, repo state.
- Writes `.viabe/queue/VT-XXX/plan.md` with proposed approach + file changes + test plan + estimated tokens.
- Sets `status: review`.
- Writes `.running/to-cowork/<ts>-plan-ready-VT-XXX.md`.

**T+0:10 — Cowork reviews plan**
- Scans `.running/to-cowork/`, finds the plan-ready message.
- Reads `plan.md`. Checks: does it match the brief? Architectural concerns? Pillar-compliant? Tests behavioral (not source-grep)?
- If approved: writes `.viabe/queue/VT-XXX/review.md` with `APPROVED + notes`. Sets `status: implementing`. Writes `.running/to-claudecode/<ts>-review-VT-XXX.md`.
- If revisions: writes `REVISIONS + specifics`. Sets `status: planning` (revert). Same signaling.
- If architectural escalation needed: writes `.viabe/queue/VT-XXX/clau-needed.md` with the question, surfaces to Fazal via chat (or Telegram once available) for paste into Claude Chat.

**T+0:30 — Claude Code implements**
- Reads review. If revisions, addresses + replans. If approved, implements.
- Appends every decision to `task_log.md` with timestamp + rationale.
- Runs tests locally. Opens PR. Writes `.viabe/queue/VT-XXX/pr.md` with PR URL + summary.
- Sets `status: in-pr`. Writes `.running/to-cowork/<ts>-pr-ready-VT-XXX.md`.

**T+1:00 — Cowork verifies PR**
- Reads PR diff. Checks: matches plan? Tests real (not source-grep)? CI green? No concept drift? Light-mode dashboard preserved (if dashboard touched)?
- If clean: writes Daily Brief entry, surfaces to Fazal via chat/Telegram: "PR #N ready to merge for VT-XXX."
- If issues: opens PR comments, sets `status: planning` or `implementing` depending on severity.

**T+1:15 — Fazal merges**
- Reviews Cowork's notes + the PR. Merges (this is the only Fazal touch in the routine cycle).
- GitHub Action fires CI on main; Cowork's daily-brief picks it up tomorrow.

**T+1:20 — Cowork closes the loop**
- Sets `status: merged`.
- Verifies post-merge artifacts (Notion updates if any, doc-update PRs filed if needed, follow-up VT rows rostered).
- Moves `.viabe/queue/VT-XXX/` to `.viabe/queue/done/`.
- Sets `status: done`. Updates dashboard.

**Total Fazal-touch in the routine cycle:** start Claude Code (until daemon exists), click merge. That's it. No relay, no question answering, no manual brief writing.

## 6. Notion → GitHub migration

**Goal:** consolidate fast-moving state into git so Cowork doesn't pay Notion-MCP latency on every audit. Notion remains for read-mostly concept docs.

**What moves to GitHub:**

| Today (Notion) | Tomorrow (GitHub) |
|---|---|
| `ViabeTeam_Sprint` board (VT rows, status, assignees, area, sprint, priority) | GitHub Project (labels = area/sprint/priority/assignee; columns = status) |
| Task ID auto-increment | Issue numbers (native) |
| Sub-task relations | Sub-issues or task lists in parent issue body |

**What stays in Notion:**

| | Why |
|---|---|
| `Clau_Session_Log` (for now) | Clau writes here today; moving = breaking Clau's workflow. Move when Clau adopts a `.viabe/log/` file pattern (likely Phase 3). |
| Concept doc, Architecture diagrams, Execution plan, 121-subtask audit | Read-mostly, lawyer-friendly UI, broad-access. Notion is the right tool for these. |
| Viabe_Launch_Tracker | Phase 1 launch tracking; revisit post-launch. |

**Migration plan (one-time, ~2-4 hours of Claude Code work):**

1. Export all VT rows from `ViabeTeam_Sprint` via Notion MCP. Capture: Task, Status, Sprint, Type, Area, Priority, Assignee, Parent item, Expected Outcome, Notes, Task ID.
2. Create a GitHub Project under `rkecom-in/viabe-team`. Define fields: Status (single-select), Sprint (single-select), Area (multi-select), Priority (single-select), Assignee (text — until GitHub Users assigned).
3. For each VT row, create a GitHub Issue with:
   - Title: `<Task>` (preserves the row's Task field)
   - Labels: `sprint:<sprint>`, `area:<area>`, `priority:<priority>`, `type:<type>`
   - Body: Expected Outcome + Notes + link back to Notion row (preserves provenance)
   - Project: Viabe Team (with Status set from Notion)
4. Create a Notion read-only mirror entry pointing to GitHub for each migrated row (so old links don't break).
5. Cowork's daily brief + dashboard switch to reading GitHub Issues instead of Notion VT rows.
6. Branch protection on PRs that reference GitHub Issue numbers — closes loop.

**When to migrate:** kicked off after the first 1-2 successful task runs under the new model. Risk-wise, doing this while ALSO bootstrapping the queue system is a lot at once. Sequence: prove the queue works → migrate the board.

## 7. Today's kickoff sequence

Concrete actions, time-boxed. All today.

**Now (T+0:00) — Cowork (me):**
- ✅ Write this plan to `.viabe/automation-plan.md`. (Done by the time you're reading this.)
- ✅ Write the formal protocol spec to `.viabe/protocol.md`. (Compressed restatement of Sections 2-3 above for Claude Code consumption.)
- ✅ Create `.viabe/queue/VT-OIV/` (owner_inputs verification) with `brief.md` + `status: queued`.
- ✅ Create `.running/` directory structure with `README.md` explaining the protocol.
- ✅ Add `.running/` to `.gitignore`.

**T+0:30 — Fazal:**
- Read this plan + the brief. Approve or request changes.
- If approved, kick off Claude Code in Max effort mode using the **current watch-mode bootstrap** (kept current in `.viabe/automation-plan.md` Appendix A — includes Pillar-7 distinction between autonomous-merge-forbidden and Fazal-authorized-via-type:task-permitted).

**T+1:00 to T+2:00 — automated cycle:**
- Claude Code plans, signals, waits for review.
- Cowork reviews, approves or revises.
- Claude Code implements, opens PR.
- Cowork verifies PR.

**T+2:00 — Fazal:**
- Merge the PR if Cowork's verification is clean.

**That's the first run.** If it goes clean, we queue the rest of the CL-391/CL-406 follow-ups under the same flow. If it goes badly, we debrief in `.cowork` and iterate the protocol.

## 8. Outstanding builds (not blocking today's kickoff)

**Build 1 — launchd daemon (Phase 2, ~half day Claude Code work):**
- macOS LaunchAgent watches `.viabe/queue/*/status` for files containing `queued`.
- On match, fires `claude` CLI with the relevant brief.
- Removes the Fazal-kicks-off-Claude-Code step from the routine cycle.
- Logs to `.viabe/daemon.log` for auditing.
- Defer until after first 2-3 successful task runs prove the queue works manually.

**Build 2 — Telegram forwarder (Phase 2, ~1-2 hours):**
- A small Node/Python script on Fazal's laptop polls Notion (or `.viabe/queue/`) for entries with a `telegram:true` flag in frontmatter.
- Sends matching entries to Fazal's Telegram via existing VT-121 bot.
- Cowork writes `.running/to-fazal/<ts>-<reason>.md` with the message + `telegram:true`; forwarder picks up + sends + moves to `processed/`.
- Defer until after first Telegram-worthy event (PR-ready-to-merge while Fazal is away).

**Build 3 — Notion → GitHub Projects migration (Phase 3, ~half day):**
- See Section 6. Defer until queue is stable.

**Build 4 — `Clau_Session_Log` mirror to `.viabe/log/` (Phase 4):**
- Allows Clau to write session-log entries as markdown files in repo.
- Removes Notion-MCP latency from Cowork's session-log audits.
- Requires Clau to adopt the new pattern; coordinate when other things are stable.

## 9. Open risks + how we'll catch them

1. **Cowork misclassifies architectural vs routine.** Mitigation: default-to-escalation; partition list in Section 1 is conservative; any PR touching a Pillar-tagged file, a migration, or a model config auto-escalates to Clau regardless of classification.
2. **Claude Code burns budget on a confused brief.** Mitigation: hard cap (250K tokens / 60 min); on cap-hit, CC stops + signals + Cowork reviews partial work.
3. **First-run failure modes.** Mitigation: first task (owner_inputs verification) is well-scoped, Step-0 done, spec matches code. Low-risk choice for the test case.
4. **Light-mode dashboard regression.** Mitigation: Section 6 of the scheduled-task prompt has the hard checklist; memory `feedback_dashboard_light_mode.md` codifies the rule.
5. **Notion bottleneck on Cowork's own audits.** Mitigation: Section 6 migration when queue is stable; until then, accept the latency.

## 10. Version history

- v1.0 (2026-05-24): Initial lock. Fazal accepted the architecture; Cowork wrote the plan.
- v1.1 (2026-05-24): Added `type: task` + `type: task-result` + `type: guidance` + `type: notify` signal types. Bootstrap revised to distinguish autonomous-merge (forbidden) from Fazal-authorized merge via `type: task` (permitted). See Appendix A.

## Appendix A — Current watch-mode bootstrap (paste into `claude` interactive session)

> Read `/Users/fazalkhan/development/viabe-team/.viabe/protocol.md` first. Disciplines in there override anything ambiguous below.
>
> Enter CONTINUOUS WATCH MODE. Do NOT exit until I type "stop watching".
>
> **Watch loop iteration:**
> 1. `ls .running/to-claudecode/*.md 2>/dev/null | sort` — process oldest first; dispatch by `type` per protocol; move each to `.running/processed/`.
> 2. `ls .viabe/queue/*/status 2>/dev/null` — for each containing `queued`, pick oldest by created, do Step-0 + plan.md + status `review` + signal `plan-ready`.
> 3. Merge-detection: for any task with status `in-pr`, `gh pr view <N> --json mergedAt`. If merged, flip status `in-pr → merged → done`, move queue dir to `done/`, unblock any task with matching `depends_on:`.
> 4. `sleep 30`, loop.
>
> **Parallelism:** never start a `queued` task if any other task is `planning` or `implementing`. Signals for `review`/`in-pr`/`done` tasks process anytime.
>
> **Pillar 7 — merges:**
> - Autonomous merge (machine decides): FORBIDDEN.
> - Fazal-authorized via `type: task` with `authorized_by: fazal`: PERMITTED, execute.
> - Each `type: task` is independently authorized — no session-wide blanket approval.
> - Missing `authorized_by: fazal` → refuse, signal `result: blocked`.
> - Command outside allowed scope (PR merges, branch ops, status flips, GH UI, single-command bash, cleanup) → refuse, signal `result: blocked`.
>
> **Other hard rules:**
> - Budget caps from brief frontmatter (default 250K tokens / 60 min); on 80%, signal `blocked`, continue loop for others.
> - Behavioral tests only — no `inspect.getsource`, no transform copies.
> - PR titles end with `(VT-XXX)`.
> - Append every decision to `task_log.md`.
> - Questions → Cowork via `.running/to-cowork/` with `type: question`. NEVER directly to Fazal.
> - Dashboard light-mode lock preserved on any artifact touch.
> - Never modify the brief — only plan/log/pr.
> - Never start `deferred` or `blocked` tasks.
>
> **Signal type handling:**
> - `type: notify` — echo body in terminal, move to processed. Telegram push is Cowork-side curl, not your job.
> - `type: guidance` — echo body, apply advice to current/future work, move to processed. No result signal needed.
>
> **Re-read protocol every ~20 iterations** (~10 min wall time) to pick up updates.
