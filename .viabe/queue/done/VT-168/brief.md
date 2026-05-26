---
task: VT-168
vt_row: 36a387c2-cc5a-81ae-ae46-f8281e4ec9cb
title: ci.yml — add `edited` to pull_request types so title-edit-only changes re-trigger CI
priority: high
type: infrastructure
area: devops
assignee: pipeline-engineer
sprint: hardening
parent: VT-166
budget_tokens: 100000
budget_minutes: 30
created: 2026-05-24T23:35:00+05:30
---

# Brief — VT-168

## Why

During the VT-166 merge (PR #54), the `pr-title` workflow ran once against the original title `(VT-AGENTSDK-LOOP)`, failed correctly, then never re-evaluated after we renamed to `(VT-166)`. The workflow trigger is:

```yaml
on:
  pull_request:
    branches: [main]
```

`pull_request:` without an explicit `types:` list defaults to `[opened, synchronize, reopened]` — **it does not include `edited`**. So title fixes never re-trigger anything.

Workarounds attempted: `gh run rerun --failed` (reuses original event payload, no help), `--admin` bypass (blocked by Repository Rulesets), close+reopen (messy PR history). The only clean path was an empty commit to force `synchronize`. Three retries.

## What

One-line change to `.github/workflows/ci.yml`:

```yaml
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, edited]
```

Reference: https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#pull_request

## Acceptance criteria

1. `.github/workflows/ci.yml` `pull_request:` block has `types: [opened, synchronize, reopened, edited]`.
2. PR title ends with `(VT-168)`.
3. After this PR merges, editing only the title of any future PR triggers a fresh CI run on the existing HEAD sha.
4. The `pr-title` check evaluates against the current (edited) title, not the original.

## Test plan

Manual smoke (post-merge):
1. Open a throwaway PR with a deliberately broken title (no VT-ID).
2. Confirm `pr-title` check fails.
3. Edit the PR title to add `(VT-168)`.
4. Confirm a fresh `pr-title` check fires within ~1 min and passes.
5. Close PR (no merge needed).

No new unit tests — the change is workflow config; behaviour is validated by GitHub Actions itself.

## Scope guardrails

In scope: the one-line `types:` addition.

Out of scope:
- Workflow structure rewrites.
- New CI checks.
- Ruleset config changes (Fazal-only, via GitHub UI).
- Adding `edited` to any OTHER workflow file (audit those separately if needed).

## Budget

100K tokens / 30 min. Should land in well under both.

## Pillar 7

Standard merge flow — Fazal's `type: task` with `authorized_by: fazal` required for the merge command itself. The brief → plan → review → implement → PR steps are autonomous (under the agent-loop daemon).

## Cross-refs

- Parent friction: VT-166 / PR #54 squash sha `3921e4f`.
- Failed merge attempts: `.running/processed/20260524T172050Z-task-result-fix-and-merge-pr54.md` (stale-payload diagnosis), `20260524T174149Z-task-result-admin-merge-pr54.md` (Ruleset bypass-deny).
- Successful merge: `.running/processed/20260524T175239Z-task-result-empty-commit-merge-pr54.md` (Option A worked).
