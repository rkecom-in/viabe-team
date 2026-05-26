# Viabe Team Protocol — for Claude Code

**Read this FIRST before executing any brief in `.viabe/queue/`.**

This document is the operating contract you (Claude Code) follow under the automation architecture locked 2026-05-24. The full architecture is in `.viabe/automation-plan.md`; this is the compressed action-protocol for the development cycle.

## Roles

- **You (Claude Code):** read briefs from `.viabe/queue/`, plan, await Cowork plan review, implement on approval, open PR, append decisions to `task_log.md`. Surface clarifications to Cowork via `.running/to-cowork/` — never to Fazal directly.
- **Cowork (delivery captain):** drafts briefs, reviews plans, audits PRs. Reachable via `.running/to-cowork/` for clarifications.
- **Fazal (CEO):** Type-3 decisions, PR merge button. You do NOT escalate to Fazal directly.
- **Clau (architect-on-call):** consulted by Cowork via Fazal-paste for architectural questions only. You do NOT contact Clau directly.

## When you receive a brief

A brief is signalled by a new file in `.running/to-claudecode/` with `type: brief-ready` AND a directory `.viabe/queue/VT-XXX/` containing `brief.md` and `status: queued`.

### Step 0 — Ground-truth check (always)

Before planning:
1. `git fetch && git log --oneline -10` — confirm HEAD matches what brief assumes.
2. Read referenced files in the brief; confirm they exist as described.
3. If brief references session-log entries (`CL-XXX`), fetch them via Notion MCP to confirm context.
4. If anything in the brief is contradicted by ground truth, write `.running/to-cowork/<ts>-blocked-VT-XXX.md` with `type: blocked` + specific contradiction. Set `status: blocked`. Stop.

### Step 1 — Plan

Write `.viabe/queue/VT-XXX/plan.md` containing:

```markdown
---
task: VT-XXX
author: claudecode
ts: <ISO>
estimated_tokens: <number>
estimated_minutes: <number>
---

## Approach
[3-5 sentences on the approach]

## File changes
[list of files you'll touch + nature of change]

## Test plan
[behavioral tests you'll write, real tests not source-greps]

## Risks
[what could go wrong, what you'll check during implementation]
```

Set `status: review`. Write `.running/to-cowork/<ts>-plan-ready-VT-XXX.md` with `type: plan-ready`. Wait for review.

### Step 2 — Wait for review

Poll `.running/to-claudecode/` for a file with `type: review` and `task: VT-XXX`.

- `APPROVED`: proceed to Step 3.
- `REVISIONS`: address the specifics in the review, rewrite `plan.md`, signal `plan-ready` again, back to Step 2.

### Step 3 — Implement

- Make the changes. Run tests locally. Make CI green.
- Open the PR. Use the branch naming convention from the brief or default `feat/vt-XXX-<short>`.
- Append every decision to `.viabe/queue/VT-XXX/task_log.md`:

```markdown
[2026-05-24T15:30:00+05:30] DECISION: chose approach X over Y because Z.
[2026-05-24T15:45:00+05:30] CHECK: ran `pytest tests/unit -v`, 180 passed.
[2026-05-24T16:00:00+05:30] DECISION: skipped refactor of helper Y as out-of-scope per brief.
```

- Write `.viabe/queue/VT-XXX/pr.md` with PR URL + 3-line summary.
- Set `status: in-pr`. Signal `.running/to-cowork/<ts>-pr-ready-VT-XXX.md` with `type: pr-ready`.

### Step 4 — Cowork verifies

Cowork reviews the PR. If issues, you'll receive `type: review` with `REVISIONS` again — address and signal back.

If clean, Fazal merges. Cowork sets `status: merged` and moves the task to `.viabe/queue/done/`.

## Communication protocol

### Inbox/outbox

```
.running/to-cowork/        # YOUR outbox (Cowork reads)
.running/to-claudecode/    # YOUR inbox (Cowork writes here)
.running/processed/        # both sides move processed messages here
```

### Message format

One file per message, timestamped, frontmatter:

```markdown
---
from: claudecode | cowork
to: cowork | claudecode
task: VT-XXX
type: question | answer | plan-ready | plan-updated | review | brief-ready | pr-ready | pre-merge-check | pre-merge-result | done | blocked | cite
ts: 2026-05-24T15:30:00+05:30
---

Body — markdown. Be specific. Reference file paths + line numbers.
If asking a question, state what you already tried.
```

### Signal types

- **notify** (cowork → claudecode): a Fazal-facing message Claude Code's watch loop must surface across all viable channels. Body format: `[COWORK NOTIFY] <message>`. Frontmatter MAY include `priority: high | normal`.
  
  Action sequence on receipt (REVISED 2026-05-24 after discovering Cowork's sandbox blocks network + osascript):
  1. **ECHO body literally in terminal** so Fazal sees it.
  2. **If `priority: high`** — fire macOS notification + Telegram push FROM CLAUDE CODE's machine (Claude Code has full network + osascript access; Cowork sandbox does NOT, so this dispatch MUST happen here):
     ```bash
     # macOS notification (works because Claude Code runs on Fazal's Mac)
     osascript -e 'display notification "<body>" with title "Cowork" subtitle "<task-id>"'
     
     # Telegram push via direct Bot API (subshell-isolate the secrets)
     TG=/Users/fazalkhan/development/viabe-team/.viabe/secrets/telegram.env
     if [ -f "$TG" ]; then
       ( set -a; source "$TG"; set +a
         curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
           --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
           --data-urlencode "text=[Cowork] <body>" > /tmp/tg-resp.json
         grep -q '"ok":true' /tmp/tg-resp.json \
           && echo "TG sent" >> /Users/fazalkhan/development/viabe-team/.viabe/notifications/log \
           || echo "TG failed: $(cat /tmp/tg-resp.json)" >> /Users/fazalkhan/development/viabe-team/.viabe/notifications/log
       )
     fi
     ```
  3. Move signal to processed/.
  
  **Why this changed:** Earlier protocol assumed Cowork-side curl would work. Verified 2026-05-24 that Cowork's scheduled-task sandbox blocks both `osascript` and outbound network — the poller's macOS+Telegram dispatches no-op silently. Only the notify-signal-to-Claude-Code path actually reaches Fazal, AND only because Claude Code's watch loop runs on Fazal's Mac with full host access. So CC is now the dispatcher for high-priority pushes. Cowork's job is to WRITE the notify signal with `priority: high`; CC's job is to ECHO + macOS + Telegram.
  
  **Claude Code Telegram plugin remains unsuitable** — it's reply-only (needs inbound chat_id). The direct Bot API curl from CC's bash tool is the proven path.
- **task** (cowork → claudecode): a SHORT-FUSE Fazal-authorized command needing execution + a specific result back. Frontmatter MUST include `authorized_by: fazal`, `issued_at: <ISO>`, `command:` (shell or NL instruction), `expected_output:` (what to report back). Used for: PR merges via `gh pr merge <N>`, branch ops, status flips on queue items, GitHub UI work, single-command bash, cleanup. NOT used for: substantive code changes (use brief/plan/review flow), architectural decisions (escalate to Clau), Type-3 decisions (Fazal acts directly). Execute, capture stdout+stderr+exit code, signal back `type: task-result`. Move to processed/.
- **task-result** (claudecode → cowork): result of a `task` execution. Frontmatter MUST include `task_signal: <filename>`, `result: success | failed | error`, `exit_code:`. Body: stdout/stderr summary + any verification (e.g., for merge: confirm via `gh pr view <N> --json mergedAt`).
- **guidance** (cowork → claudecode): a Fazal-authorized ADVISORY message — info, correction, hint, or directive that affects current/future behavior but does NOT need a specific execution result. Frontmatter: `authorized_by: fazal`, `issued_at: <ISO>`, `applies_to:` (task ID OR "all" OR "next"). Body: the guidance text in natural language. Examples: "stop work on VT-X, it's been deferred", "re-read .viabe/protocol.md, it just changed", "the env var name is ANTHROPIC_API_KEY not ANTHROPIC_KEY, retry the canary", "Clau updated CL-XXX, read it before proceeding". Action: acknowledge by ECHOING the body in terminal, apply the guidance to current/future work, MOVE to processed/. No `guidance-result` signal needed — the effect is observable in what Claude Code does next. If Cowork wants confirmation, it can ask via `type: question` after.
- **brief-ready** (cowork → claudecode): a new brief is queued; pick it up and Step-0 + plan.
- **plan-ready** (claudecode → cowork): plan.md written; awaiting review.
- **review** (cowork → claudecode): plan reviewed; verdict APPROVED / APPROVED-with-condition / REVISIONS. See `verdict:` frontmatter field.
- **plan-updated** (claudecode → cowork): plan revised after REVISIONS; re-review.
- **pr-ready** (claudecode → cowork): PR opened; awaiting Cowork verification + Fazal merge.
- **pre-merge-check** (cowork → claudecode): run a specified test/canary, capture output, append result summary to the PR description. Frontmatter MUST include `command:` (the shell command to run), `output_file:` (where to capture stdout+stderr), `pr_url:` (the PR to append a summary to), `env_required:` (list of env vars needed; if any missing, signal `blocked`). After execution, signal `pre-merge-result`.
- **pre-merge-result** (claudecode → cowork): canary/test executed; result is `passed`/`failed`/`error`. Body includes summary + path to full log.
- **question** (claudecode → cowork): clarification needed; Cowork answers or escalates.
- **answer** (cowork → claudecode): response to a question.
- **cite** (claudecode → cowork): instead of adding inline coverage, citing an existing test that covers the gap.
- **done** (claudecode → cowork): task fully closed; queue dir can move to done/.
- **blocked** (either direction): something prevents progress; surface to Fazal.

### Processing semantics

- On startup or when waiting: scan your inbox dir, process oldest first.
- After acting on a message, MOVE it (not delete) to `.running/processed/`.
- No race conditions: each message is its own file, atomic.
- Never empty a file in-place.

## Session restart + crash recovery

If your terminal closed / Mac restarted / Claude Code session was killed mid-task, recover as follows:

1. Resume the same conversation if possible: `cd <repo> && claude -c` (continues most recent). If you need a specific older session, `claude --resume <session-id>` using the ID from `.viabe/daemon/session.state`.
2. If brand-new session: paste the watch-loop bootstrap from `.viabe/automation-plan.md` Section 7.
3. **Reconcile state from filesystem before doing anything.** Read `.viabe/queue/*/status`. For any task in `planning` or `implementing`:
   - `git status` in the repo to detect partial uncommitted work
   - Read the task's `plan.md` + `task_log.md` to understand last action
   - Decide: continue if safe (no merge conflicts, plan still valid); else signal `type: blocked` to Cowork with diagnosis
4. Once reconciled, enter the watch loop normally.

For Cowork-in-chat post-restart: read MEMORY.md + `.viabe/notifications/cowork-catchup.log` + queue state + git HEAD. Reference current state in your first response so Fazal knows you've recovered.

## Hard rules

1. **Budget caps.** When you start, note the cap (default 250K tokens / 60 min). When you've used 80%, stop new work, finish what's in progress, signal `type: done` or `type: blocked` to Cowork. Do not silently exceed.
2. **Pillar compliance.** Never put LLM calls in the deterministic pre-filter subtree. Never add a body column to `owner_inputs`. Never bypass branch protection. Never skip CI gates.
3. **Behavioral tests, not source-greps.** A test that uses `inspect.getsource` or copies a production transform into the test file is not behavioral. Real tests invoke the production code path.
4. **VT-ID before code.** Every PR title ends with `(VT-XXX)`. Every commit references the VT-ID. If no VT row exists for the work, stop and signal Cowork — never invent work off-the-board.
5. **Task log is append-only.** Every decision goes in. Cowork can read it post-completion to understand your reasoning.
6. **No escalation to Fazal.** All questions go to Cowork via `.running/to-cowork/`. Cowork either answers or escalates further.
7. **Light-mode dashboard.** If you touch the PM dashboard artifact, follow the light-mode lock in `feedback_dashboard_light_mode.md` (Cowork's memory). Grep-verify before save.

## Reference paths

- Repo: `/Users/fazalkhan/development/viabe-team/`
- Queue: `.viabe/queue/`
- Protocol: `.viabe/protocol.md` (this file)
- Plan: `.viabe/automation-plan.md`
- Inbox: `.running/to-claudecode/`
- Outbox: `.running/to-cowork/`
- Processed: `.running/processed/`
