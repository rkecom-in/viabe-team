---
task: VT-168
author: claudecode
ts: 2026-05-24T23:36:00+05:30
estimated_tokens: 8000
estimated_minutes: 10
---

## Approach

Add `types: [opened, synchronize, reopened, edited]` to the `pull_request:` trigger block in `.github/workflows/ci.yml`. Single-line addition (line 4 before `branches: [main]`). The default `pull_request:` event set is `[opened, synchronize, reopened]`; adding `edited` covers PR title and description changes so `pr-title` (and any other check that reads `github.event.pull_request.title`) re-runs against the current state. No other changes — brief explicitly scopes this to `ci.yml` only.

## File changes

- `.github/workflows/ci.yml` — add `types:` line to the `pull_request:` block. Single insertion, no deletions, no semantic change to job definitions.

## Test plan

The change is workflow config — behaviour is validated by GitHub Actions itself. No new unit tests. Local verification: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` to confirm YAML still parses. The brief specifies a manual post-merge smoke (throwaway PR, edit title, watch fresh `pr-title` fire) — that's a Fazal-side check after merge.

## Risks

1. **YAML indentation drift.** GitHub Actions is strict; the `types:` key must sit at the same indent as `branches:`. Fix: copy the brief's exact form (`types: [opened, synchronize, reopened, edited]`) — flow-style list, single line.
2. **Concurrency cancellations multiplying.** `concurrency.group: ci-${{ github.ref }}` + `cancel-in-progress: true` already in the file. Now that title edits also fire workflows, an in-flight CI run gets cancelled if Fazal edits the title mid-run — desired behaviour, no change needed. Worth flagging.
3. **Other workflows still default-trigger.** Brief explicitly excludes auditing other workflows in this scope. If `.github/workflows/*.yml` has additional files with `pull_request:` triggers that should also catch title edits, that's a follow-up task.
4. **`edited` fires on every title/body keystroke from the GitHub UI form (debounced server-side).** Worst case: a manual title polish triggers 1-3 wasted CI runs. Acceptable cost given the alternative is "couldn't merge cleanly without ceremony" — which we just lived through.
