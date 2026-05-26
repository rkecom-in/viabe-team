# Claude Code Watch-Mode Bootstrap

**Pinned location.** Paste the block below into a fresh `claude` interactive session to arm the watch loop.

**Quickest paste path (macOS):**
```bash
sed -n '/^> Read/,/^> \*\*Re-read protocol/p' /Users/fazalkhan/development/viabe-team/.viabe/BOOTSTRAP.md | sed 's/^> //; s/^>$//' | pbcopy
```
Then `claude -c` (resumes most recent conversation) or `claude` (fresh), and ⌘V to paste.

**Or one-shot copy + open Claude Code:**
```bash
bash /Users/fazalkhan/development/viabe-team/.viabe/bootstrap-copy.sh && claude -c
```

---

## The bootstrap (current as of 2026-05-24 v1.1)

> Read `/Users/fazalkhan/development/viabe-team/.viabe/protocol.md` first. Disciplines in there override anything ambiguous below.
>
> Enter CONTINUOUS WATCH MODE. Do NOT exit until I type "stop watching".
>
> **Watch loop iteration:**
> 1. `ls /Users/fazalkhan/development/viabe-team/.running/to-claudecode/*.md 2>/dev/null | sort` — process oldest first; dispatch by `type` per protocol; move each to `.running/processed/`.
> 2. `ls /Users/fazalkhan/development/viabe-team/.viabe/queue/*/status 2>/dev/null` — for each containing `queued`, pick oldest by created timestamp, do Step-0 + plan.md + status `review` + signal `plan-ready`.
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
> - `type: notify` — echo body in terminal. **If `priority: high`** also fire macOS notification AND Telegram push from your Mac (Cowork sandbox can't, but you can):
>   - `osascript -e 'display notification "<body>" with title "Cowork" subtitle "<task-id>"'`
>   - Telegram via direct Bot API curl, sourcing `.viabe/secrets/telegram.env` in a subshell. Full snippet in `.viabe/protocol.md` notify section.
>   - Then move to processed.
> - `type: guidance` — echo body, apply advice to current/future work, move to processed. No result signal needed.
> - `type: task` (with `authorized_by: fazal`) — execute the command, signal `task-result` with stdout/exit code + verification.
>
> **Re-read protocol every ~20 iterations** (~10 min wall time) to pick up updates.

---

## How to verify the watch loop is alive

In another terminal:
```bash
tail -f /Users/fazalkhan/development/viabe-team/.viabe/daemon/watch.log   # legacy bash watcher (if used)
# Or just watch the queue/inbox state:
watch -n 5 'echo "=== Queue ===" && for s in /Users/fazalkhan/development/viabe-team/.viabe/queue/*/status; do echo "$(dirname $s | xargs basename): $(cat $s)"; done && echo "" && echo "=== Inbox ===" && ls /Users/fazalkhan/development/viabe-team/.running/to-claudecode/'
```

## To exit cleanly

In the watch-loop chat, type: `stop watching`.

## Updates to this file

When the bootstrap changes (new signal types, new disciplines), Cowork updates this file. To diff against the version you last pasted: `git log -p .viabe/BOOTSTRAP.md`.
