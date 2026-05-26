---
pr_url: https://github.com/rkecom-in/viabe-team/pull/54
branch: feat/vt-agentsdk-loop-daemon
sha: dc09638
opened_at: 2026-05-24T22:25:00+05:30
---

# PR #54 — VT-AGENTSDK-LOOP

URL: https://github.com/rkecom-in/viabe-team/pull/54
Title: `feat(infra): agent-loop daemon — Python Agent SDK orchestrator (VT-AGENTSDK-LOOP)`
Branch: `feat/vt-agentsdk-loop-daemon` → `main`
Commit: `dc09638`

## 3-line summary

Replaces the Phase-1 bash watcher with a Python daemon that embeds `claude_agent_sdk.query()` and maintains one persistent Claude Code session across every signal; enforces Pillar 7 in code via a `PreToolUse` hook on `gh pr merge` and the four cowork-review conditions are addressed inline (SDK-error backoff, `notify` short-circuit, per-task token budget gate, `_active_signal_context` try/finally clearing); 27 pytest tests + ruff clean. Live install/run step is Fazal-driven post-merge (`bash .viabe/daemon/install-launchd.sh`).
