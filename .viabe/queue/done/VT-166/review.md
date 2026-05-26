---
reviewer: cowork
verdict: APPROVED-with-condition
ts: 2026-05-24T22:07:30+05:30
---

Verdict: APPROVED-with-condition. 4 inline conditions, 3 non-blocking notes. Cleared to Step 3.

## Conditions (addressed inline during implementation)

1. **SDK-error backoff** on `query()` exceptions — try/except, sleep 5s+30s retry ladder, write `type: blocked` to cowork on persistent failure, leave signal in inbox, record `cost=$0.00 error=<class>` line in `cost.log`.
2. **Short-circuit `type: notify`** (and other no-LLM types) — direct echo + DAEMON_LOG append + processed/ move, no `query()` call. Maintain explicit `LLM_REQUIRED_TYPES` set; anything outside short-circuits.
3. **Budget reconciliation**: per-call `max_budget_usd=5.0` (SDK hard cap) + per-task aggregation from `cost.log` (`sum tokens for task_id`) checked against `<task>/brief.md`'s `budget_tokens`, blocking dispatch at ≥80%.
4. **`_active_signal_context` discipline** — thread-safety warning comment + `try/finally` clearing in `process_signal` so backoff retries can't leak a stale context.

## Non-blocking notes

- PR title literal (em-dash): `feat(infra): agent-loop daemon — Python Agent SDK orchestrator (VT-AGENTSDK-LOOP)`.
- `pre_compact_archive`: verify `~/.claude/projects/<encoded-cwd>/<session>.jsonl` exists; fallback to scan + log warning + skip.
- `pr-ready` signal frontmatter: include `pr_url:` for Cowork's auto-merge-detection.

## Q&A summary

- Plan-risk #1 (`feedback_snapshot_sequencing.md`) — dropped; that memory is about Cowork↔Clau snapshot mirroring, no conflict with daemon protocol.
- Cost-budget enforcement at daemon-level is correct (SDK exposes only per-call cap).
- `$5.00`/signal default is fine; document per-type ceiling.
- Idempotency asymmetry confirmed; merge-detection self-heals if daemon crashes after a successful `gh pr merge` but before signalling task-result.

## Process

TDD preferred: write `test_pick_next_action.py` + `test_hooks.py` before `agent-loop.py` / `hooks.py`.
