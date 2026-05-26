---
task: VT-MIG-SPRINT
vt_row: (no Notion row — this task IS the Notion-to-files migration)
author: cowork
ts: 2026-05-24T16:00:00+05:30
budget_tokens: 250000
budget_minutes: 60
priority: High
sprint: Hardening
area: Infrastructure · Documentation
assignee: claudecode
parent: VT-EngineeringReference (356387c2-cc5a-81c8-8f55-db365c759a92)
depends_on: VT-OIV (don't start until owner_inputs verification PR is merged)
---

# Brief — migrate ViabeTeam_Sprint board to `.viabe/sprint/` markdown files (VT-MIG-SPRINT)

## Why this task

Notion-MCP latency is the bottleneck on Cowork's daily audit work (~hundreds of ms per call, semantic-search misses, ~400+ entries across two databases). Moving the sprint board into the repo as markdown files removes that latency entirely — Cowork reads via `Read` + `Grep` (millisecond ops), the data is branch-protected, supersession chains become visible in `git log`, and the dashboard refresh becomes near-instant.

Notion remains canonical for read-mostly content (concept doc, architecture diagrams, execution plan, 121-subtask audit, Clau_Session_Log for now). Only `ViabeTeam_Sprint` migrates in this task.

Per `.viabe/automation-plan.md` Section 6 — this is the Phase 2 migration plan. Session log migration is deferred to post-launch.

## Goal

Produce a complete, validated set of `.viabe/sprint/VT-<N>.md` files — one per row in Notion `ViabeTeam_Sprint` — with full metadata preserved as YAML frontmatter and original body content preserved as markdown sections. Cross-link each Notion row to its new file location. Cowork can read sprint state from filesystem after this lands.

## Step-0 ground-truth check (do before planning)

1. `git fetch && git log --oneline -5` — confirm HEAD matches what brief assumes; capture the SHA for cross-link references.
2. Confirm `.viabe/sprint/` does NOT already exist (this is a fresh migration). If it exists, surface to Cowork via `.running/to-cowork/<ts>-blocked-VT-MIG-SPRINT.md` — DO NOT overwrite.
3. Count rows in Notion `ViabeTeam_Sprint` (data source `collection://20c8c0cc-7ba5-41cb-999e-77246cdefc51`). Expected ~150 (~121 originals + ~30 added in last 48h). Capture exact count for validation.
4. Fetch the data source schema (`notion-fetch` on the collection URL) to confirm field names + types haven't changed.

## Detailed scope

### Data to export per row

From the `ViabeTeam_Sprint` data source schema (verified 2026-05-23):

| Notion field | YAML key | Type | Notes |
|---|---|---|---|
| Task | `task` | string | The row title |
| Task ID | `id` | string | `VT-<N>` format; this becomes the filename |
| Status | `status` | enum | one of: backlog \| to-do \| queued \| in-progress \| review \| done \| blocked \| deferred |
| Sprint | `sprint` | string | as-is from Notion enum |
| Type | `type` | string | feature \| hotfix \| bugfix \| infrastructure \| documentation |
| Area | `area` | list[string] | Notion multi-select → YAML array |
| Priority | `priority` | string | critical \| high \| medium \| low |
| Assignee | `assignee` | string | Pipeline Engineer \| Frontend Engineer \| Quality Engineer \| Hotfix Engineer \| Test Engineer \| Fazal \| Clau |
| Parent item | `parent` | string | `VT-<N>` of parent; null if no parent |
| Sub-item | `children` | list[string] | `[VT-<N>, VT-<M>]`; empty list if no children |
| Date | `date` | ISO date | null if not set |
| Branch | `branch` | string | null if not set |
| Version | `version` | string | null if not set |
| Expected Outcome | (body section `## Expected Outcome`) | markdown | |
| Notes | (body section `## Notes`) | markdown | |
| (page body) | (body section `## Original Notion body`) | markdown | only if non-empty |
| (Notion metadata) | `notion_id`, `notion_url`, `created`, `migrated` | strings | provenance |

### Output file shape

`.viabe/sprint/VT-<N>.md`:

```markdown
---
id: VT-101
task: "Owner_inputs feature verification before flag-flip"
status: queued
sprint: Hardening
type: feature
area: [Knowledge Architecture, Privacy]
priority: critical
assignee: Quality Engineer
parent: VT-7
children: []
date: null
branch: null
version: null
notion_id: 369387c2-cc5a-8142-8260-daf45fd6ab94
notion_url: https://www.notion.so/369387c2cc5a81428260daf45fd6ab94
created: 2026-05-23T19:19:00.000Z
migrated: 2026-05-24
---

## Expected Outcome

<copy of Notion Expected Outcome field>

## Notes

<copy of Notion Notes field>

## Original Notion body

<copy of Notion page body, if non-empty>
```

### Pagination + rate handling

Notion MCP search/query may return paginated results. Use the cursor pattern; process all pages. If rate-limited, exponential backoff. Total expected: ~150 fetches. Should complete well under the token cap.

### Cross-link back to Notion

For each migrated row, update the original Notion row by appending one line to its Notes field:

```
Migrated to .viabe/sprint/VT-<N>.md as of <commit-sha> 2026-05-24. Subsequent edits in repo.
```

Use `notion-update-page` with `update_content` command. Idempotent: check if the line already exists before appending (re-running shouldn't double-append).

### Validation report

After migration, write `.viabe/sprint/MIGRATION-REPORT.md`:

```markdown
# Sprint migration report — 2026-05-24

## Counts
- Notion rows: <N>
- Files written: <N>
- Match: ✓ | ✗

## Spot-checks (5 random rows)
[for each: VT-id, field-level diff between Notion and file]

## Drift
[any rows where status/priority/parent didn't round-trip cleanly]

## Cross-link verification
- Notion rows updated with migration note: <N> of <N>
- Failures: <list>

## Issues
[any rows that needed manual intervention]
```

## Pass criteria

1. Count match: every Notion row has a corresponding `.viabe/sprint/VT-<N>.md` file.
2. Frontmatter complete: every required field present; null where Notion field was empty.
3. Filename matches `id`: `VT-101.md` contains `id: VT-101`.
4. Body sections present: Expected Outcome + Notes sections in every file (empty if Notion fields were empty).
5. Parent/children relations bidirectional: every `parent: VT-X` reference points to an existing file; every `children: [VT-Y]` reference has `parent: VT-N` in VT-Y's file.
6. Cross-link applied to all Notion rows (or list of failures in the report).
7. `MIGRATION-REPORT.md` exists and shows pass.
8. Git diff shows ~150 new files in `.viabe/sprint/`, nothing else changed unexpectedly.

## Out of scope

- Migrating `Clau_Session_Log` — deferred to a separate task (deferred to post-launch).
- Creating GitHub Issues from these files — that's Phase 1.5, optional, separate task if pursued.
- Building a Notion→files reverse sync — separate task, only if needed after Clau workflow transition.
- Cowork's switch to filesystem-first reads — Cowork does that itself after this PR merges; not Claude Code's job.
- Cleaning up Notion (archiving rows, deleting) — DO NOT delete or archive any Notion rows. Read-only operations on the source except for the single cross-link append.

## Reference materials

- Automation plan Section 6: `.viabe/automation-plan.md`
- Notion sprint board data source: `collection://20c8c0cc-7ba5-41cb-999e-77246cdefc51`
- Sprint board page: https://www.notion.so/5c7bcb44f57046f2a67cb4255c5e2f5a
- Schema reference (`notion-fetch` on the collection URL gives the full SQLite schema)
- Existing files in `.viabe/sprint/` should NOT exist before run; if any do, abort.

## Hard rules

- **Do NOT delete or archive any Notion rows.** Source must remain intact.
- **Do NOT modify the Notion schema** (no new fields, no enum changes).
- **Do NOT alter any field values** in Notion except the single append to the Notes field for cross-linking.
- **Idempotent.** Re-running must not duplicate files, must not double-append cross-links.
- VT-IDs preserved exactly. Code references like `VT-101` in the codebase must still find content via grep after migration.
- PR title: `feat(infra): migrate ViabeTeam_Sprint board to .viabe/sprint/ markdown files (VT-MIG-SPRINT)`
- Branch: `feat/vt-mig-sprint-board-to-files`

## Estimated effort

- Step-0 + planning: 15 min
- Export script: 20 min
- Run export + validate: 20 min (depends on Notion MCP latency × 150 calls; could be 30-40 min if rate-limited)
- Cross-link: 15 min (semi-automated)
- Validation report: 10 min
- PR opening + CI: 10 min
- Total: ~90 min (over the 60-min cap; budget bumped to 90 in frontmatter — if you find yourself approaching the cap, signal and we'll split or extend)

---

**When done:** signal `.running/to-cowork/<ts>-pr-ready-VT-MIG-SPRINT.md`. Cowork verifies the report + spot-checks 5 files; Fazal merges. Then Cowork switches its memory + scheduled task to filesystem-first reads in a follow-up task.
