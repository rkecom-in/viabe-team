# CC Startup & Resilience Protocol — read on EVERY (re)start

**Why this exists:** the interactive `claude -c` loop's in-session watchers/crons DIE when the CC process
restarts (MCP disconnect, machine sleep, task teardown). They do NOT self-recover. A restart therefore =
silent idle until something external catches it, and the recurring damage is signals
*archived-but-never-executed* + the shared tree left stranded on a feature branch. This protocol makes
recovery immediate and clean. (It cannot make the loop immune to process death — see "The durable fix".)

## On EVERY startup — do these IN ORDER, before any new work

1. **Return the tree to clean `dev`.** `git checkout dev && git pull`. The shared tree gets stranded on a
   feature branch when the process dies mid-PR. If uncommitted cruft blocks the switch, discard the
   regenerable substrate (sprint-brief / latest-snapshot / active-context-summary are rebuilt from git) or
   stash explicitly named files (NEVER `git stash -u` — CL-418). If `.git/index.lock` exists → **signal
   Fazal** (FUSE; only the Mac terminal can unlink it). NEVER start work on a stranded branch.

2. **Drain the FULL inbox, oldest-first.** Process EVERY signal already in `.running/to-claudecode/`. A
   signal sitting there at launch is NOT a new "arrival" — the watcher will NOT fire on it. Do not enter
   watch-mode until the inbox is empty.

3. **Reconcile before re-doing (Rule #14).** `git log origin/dev --oneline -15` — before acting on a pickup
   item, confirm it isn't already merged. Avoid double-work / double-merge.

4. **Announce liveness.** Emit a `resumed: on dev @ <sha>, inbox drained, driving <list>` status to
   `.running/to-cowork/`. Silence = assumed-dead — always announce.

## While running

5. **Archive AFTER execution, never on consumption.** Do NOT move a signal to `.running/processed/` until
   its work is actually DONE (merged, or pr-ready-and-parked at the Cowork gate). The recurring stall =
   signals archived but never executed. If a step blocks: signal the blocker + move to the NEXT item —
   never go silent holding un-executed archived signals.

6. **Re-arm FIRST on every wake.** First action on any watcher fire / heartbeat = re-arm the watcher, THEN
   drain the full inbox.

7. **Progress heartbeat.** On any task running >10 min, emit a progress status so silence ≠ stalled. Never
   hold work silently for >15 min.

8. **Tree discipline (CL-418).** Stay on `dev` when idle. Commit at PR-time on feature branches; return to
   `dev` after each PR/merge. Never leave the shared tree parked on a feature branch — it strands the next
   session and makes Cowork misread state.

## The durable fix (Fazal's standing option)

These steps make RECOVERY fast — they do NOT make failure impossible; the loop's watchers always die with
the process. The only true restart-survivors are:

- the **external Cowork poller** (15-min; it caught the last stall — keep it on; consider shortening to 5
  min), and
- the **Python daemon** (`.viabe/daemon/`, currently STOPped via the STOP file) — a background OS process
  that survives MCP disconnects/restarts. Enabling it (remove the STOP file) **and** stopping the
  interactive loop (one-or-the-other — they race the same inbox) is the structural fix. Trade-off: no live
  console output for Fazal.

**If stalls recur, switch to the daemon.** It is the only configuration that genuinely doesn't go silent
on a process restart.

## DRAIN-BEFORE-ARM (Lesson 2026-06-25 — the recurring stall's true root cause)

The watcher BASELINES whatever is in `.running/to-claudecode/` when it arms, and only fires on files NOT in
that baseline. So **if you re-arm the watcher while un-processed signals are sitting in the inbox, those
signals get baselined and the watcher NEVER fires on them — they are silently lost.** This is exactly how CC
"stalled" on 2026-06-24 ~16:00: 6 Cowork dispatches (push-go, #506-clear, e2e-data) sat unread because the
watcher was re-armed without first draining them; CC then claimed "waiting on Cowork" while Cowork had
already answered.

**The rule: DRAIN, THEN ARM.** On every wake / re-arm: (1) read EVERY file in the inbox and act/dispatch on
each (oldest-first), (2) only THEN re-arm the watcher. Never re-arm with un-read signals present. If you
re-arm with old signals still in the inbox (because their work isn't done), that's fine — but you must have
READ them already; the watcher only needs to catch FUTURE arrivals. **Verify after every re-arm:**
`ls -1 .running/to-claudecode/` and confirm you have read each file — a count that surprises you means you
skipped a drain.

## BATCH PUSHES TO origin/dev (STANDING, Fazal 2026-06-24 — see CLAUDE.md merge-workflow)

Do NOT push `origin/dev` per change (each push = a CLI deploy + Railway redeploy = cost). Integrate cleared
changes into LOCAL `dev` and HOLD; push `origin/dev` in BATCHES only on a "push" signal or a deploy/test
checkpoint. Mechanic = local-integrate the cleared branches + ONE `git push origin dev`. Feature-branch pushes
(PRs/fixes) are fine (no dev build). Per-change gate/canary/verification is UNCHANGED — only the push batches.
