# `.viabe/sprint/` — the Viabe Team sprint board (post-Notion)

Source of truth for all VT rows. Replaces the Notion ViabeTeam_Sprint board as of 2026-05-25 cutover. Notion stays read-only archive.

## File layout

```
.viabe/sprint/
├── .next-id           # text file holding the next VT-N number to issue
├── .lock              # flock target — exclusive lock for ID allocation
├── README.md          # this file (schema + protocol)
├── VT-1.md            # parent: VT-Pillars
├── VT-2.md            # parent: VT-Foundation
├── ...                # one .md file per VT row, named VT-N.md (N = numeric Task ID)
└── done/              # optional: archive of completed VT rows once stale enough to declutter
    └── VT-NN.md
```

Filename is canonical — `VT-<number>.md` exactly. The number is what Notion previously assigned as `auto_increment_id`; new rows get one via `scripts/vt_id_allocate.py`.

## Frontmatter schema

Every `VT-N.md` starts with YAML frontmatter. Fields:

```yaml
---
vt_id: VT-N                          # canonical ID; must match filename
title: short title here              # ≤80 chars
status: Backlog                      # one of: Backlog | To Do | Queued | In Progress | Review | Done | Blocked | Deferred
priority: Critical                   # one of: Critical | High | Medium | Low
sprint: Sprint 1 - Foundation        # one of the Sprint enum values (see below)
type: Infrastructure                 # one of: Feature | Hotfix | Bugfix | Infrastructure | Documentation
area: [Privacy, Database]            # JSON array, multi-select; values from Area enum
assignee: Pipeline Engineer          # one of: Pipeline Engineer | Frontend Engineer | Quality Engineer | Hotfix Engineer | Test Engineer | Clau | Fazal
parent: VT-2                         # parent row's vt_id; empty for the 16 parents
sub_items: []                        # array of child vt_ids; empty for leaf rows
exec_order: 1                        # number; within-sprint ordering hint (lower runs first)
branch: feat/vt-foundation-xyz       # planned/active branch name
version: v1.0                        # increment when the row's spec changes meaningfully
notion_legacy_id: 356387c2-...       # for traceability; immutable
created: 2026-04-15                  # ISO date
last_updated: 2026-05-25T03:30:00+05:30
done_at: 2026-05-15                  # ISO date, set when status flips to Done
shipped_in: PR #3 sha 06349c8        # set when status flips to Done; identifies the shipping PR
---
```

### Sprint enum

`Pre-Sprint 0 - Pillars & Setup`, `Sprint 1 - Foundation`, `Sprint 2 - SR Agent Skeleton`, `Sprint 3 - Ingestion Methods 1-2`, `Sprint 4 - Ingestion Methods 3-5`, `Sprint 5 - Online Methods 6-9`, `Sprint 6 - Tools Batch 2`, `Sprint 7 - Knowledge Architecture`, `Sprint 8 - Owner Surface & Billing`, `Sprint 9 - Polish & E2E`, `Hardening`, `Vendor Approvals Buffer`.

### Area enum

`Orchestrator`, `Specialist Agent`, `MCP Tools`, `Knowledge Architecture`, `Privacy`, `Ingestion`, `Owner Surface`, `Billing`, `Frontend`, `Database`, `Infrastructure`, `Observability`, `DevOps`, `Legal/Policy`, `Documentation`.

## Body schema

Below the frontmatter, free-form Markdown structured as:

```markdown
# VT-N — Title

## Why
The motivation. What product/architecture intent this row serves. Reference concept-team.md sections, Pillars, CL- entries.

## What
The concrete deliverable. File paths, function names, behaviour to achieve.

## Acceptance criteria
- Bullet list of explicit checks. Each one is a passing test or observable state.

## Out of scope
What this row does NOT do. Lists adjacent VT rows that own the excluded work.

## Notes
Architectural caveats, references, links to Clau_Session_Log entries.

## Status history
- 2026-04-15: Backlog (rostered from CL-XX)
- 2026-05-15: Done (PR #3 sha 06349c8 — feat(foundation): base migrations)
- 2026-05-25 02:30 IST: stale-Done correction (Cowork audit per Rule #14)

## Cross-refs
- Parent: VT-N (link to ../VT-N.md)
- Children: VT-N.M (link)
- Related VT rows: ...
- Concept doc: §X
- Pillars: 1, 3, 8
- CL entries: CL-XX, CL-YY
```

The Status history is append-only. Never delete a line; correct via a new entry.

## How to create a new row

```bash
# 1. Allocate ID
vt_id="$(python scripts/vt_id_allocate.py)"    # e.g. "VT-169"

# 2. Create the file from template
cat > ".viabe/sprint/${vt_id}.md" <<EOF
---
vt_id: ${vt_id}
title: Your title here
status: Backlog
priority: Medium
sprint: Hardening
type: Feature
area: [DevOps]
assignee: Pipeline Engineer
parent: VT-N
sub_items: []
exec_order: 999
branch: ""
version: v1.0
notion_legacy_id: ""
created: $(date +%F)
last_updated: $(date -Iseconds)
---

# ${vt_id} — Your title here

## Why
...
EOF
```

## How to update status

Edit the frontmatter `status:` field AND append a one-line entry to the `## Status history` section. The status change is not real until both happen — the frontmatter is for machine reads (dashboards, CI), the history is for audit.

If the change is a Cowork audit-driven correction (status was wrong, ground truth wins per Rule #14), the history line MUST include the audit identifier and the evidence ground (PR sha, file path, etc.).

## How to peek next available ID

```bash
python scripts/vt_id_allocate.py --peek
```

Doesn't consume; just reports.

## What this replaces

Notion `ViabeTeam_Sprint` data source (`collection://20c8c0cc-7ba5-41cb-999e-77246cdefc51`). After cutover, Notion is read-only. Every Cowork/CC/Clau write goes here, not to Notion. The `notion_legacy_id` field in each row's frontmatter preserves traceability to the original Notion page.

## Pillar 7 reminder

Merges still require Fazal's `type: task` authorization via the running orchestrator (interactive watch loop or daemon). The migration to file-based tracking does not relax any of the merge-authorization or audit-log disciplines from `docs/clau/operating-brief.md` and `.viabe/protocol.md`.
