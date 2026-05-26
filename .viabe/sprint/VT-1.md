---
vt_id: VT-1
title: VT-Pillars — write concept-team-pillars.md
status: Backlog
priority: Critical
sprint: Pre-Sprint 0 - Pillars & Setup
type: Documentation
area: [Documentation]
assignee: Clau
parent: ""
sub_items: [VT-15, VT-16]
exec_order: 1
branch: "docs/vt-pillars"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-8133-8167-c85012871391
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-1 — VT-Pillars — write concept-team-pillars.md

## Why this parent exists
Reports product fell into expensive drift through rounds R6→R9 because architectural commitments lived only in conversation. Fixes accumulated as patchwork — regex scrubs, hardcoded constants, value clamps — instead of routing through proper layers. Each round of fixes seeded two more leaks. Reports' `concept.md` was created retroactively, after the damage. Team product launches with `concept-team-pillars.md` from day one, before any code lands, so every CoderC commit and every CoderX review can be tested against an inviolable rulebook.
The parent's outcome is a single document that future agents and engineers can consult to resolve any architectural ambiguity. When two design decisions conflict, this document wins. When a fix proposes a hardcoded constant in a producer file, this document is the basis for rejecting it.

## What this parent owns
1. The 8 inviolable architectural pillars in canonical written form, each with rule + rationale + examples of violations + verification check.
2. Cross-references from `concept-team.md` (top-of-file note) and `CLAUDE.md` (Team product section) pointing at the pillars file.
3. The source-of-truth hierarchy: `concept-team-pillars.md` > `concept-team.md` > `CLAUDE.md` > `Viabe_Team_Technical_Reference_v1_0.md` (when written in VT-14).
4. The governance protocol declaration: pillars only change via Type 3 board action (Fazal + Clau formal sign-off, documented in Notion).

## Architectural rules binding every subtask under this parent
- Pillars are inviolable. They cannot be amended through routine PR review. Type 3 protocol applies to any change.
- Each pillar must include four sections: a one-line rule, a short rationale (the failure mode it prevents, citing Reports lessons VB-165, VB-134, etc. where applicable), an examples-of-violations section (so future agents can pattern-match), and a verification check (CI grep, test fixture, or review checklist).
- The pillars file lives at `docs/concept-team-pillars.md` in the monorepo root, mirroring the location of Reports' `concept.md`.
- Pillars must reference [concept-team.md](http://concept-team.md) sections by number where the lineage exists.
- The file uses a stable, mechanical structure so future agents can locate any pillar quickly.

## Subtasks under this parent
1. **VT-1.1** — Author `concept-team-pillars.md` v1.0 with all 8 pillars fully drafted (rule + rationale + violation examples + verification check per pillar) and the governance protocol declaration.
2. **VT-1.2** — Cross-link `concept-team-pillars.md` from `concept-team.md` (top-of-file note) and from `CLAUDE.md` (Team product section).

## Definition of done
- `concept-team-pillars.md` v1.0 committed to repo on `main` (this is the rare exception to the never-push-to-main rule — pillars must exist before any code).
- All 8 pillars present, each with the required four sections.
- `concept-team.md` and `CLAUDE.md` both reference the pillars file at their top, with clear language that this file is the highest authority for Team product architecture.
- Fazal has approved the document via Type 3 protocol, recorded in Notion.
- Source-of-truth hierarchy declared explicitly inside the pillars file itself.

## Out of scope
- No pillar implementation in code — that is spread across VT-2 through VT-13. The pillars file is descriptive of the rules, not prescriptive of implementations.
- No Engineering Reference document — that is VT-14.
- No specific implementation rules for individual MCP tools, ingestion methods, or agent prompts. Those live in their respective parents and must conform to the pillars but do not duplicate them.
- No retroactive update of Reports' `concept.md`. That is a separate Reports-side task if needed.

## Branch convention
- Parent branch: `docs/vt-pillars`.
- Subtask branches: `docs/vt-pillars-author` (VT-1.1), `docs/vt-pillars-crosslinks` (VT-1.2).
- PR title format: `docs(pillars): <description> (VT-1.N)`.
- Reviewers: Fazal must approve personally (Type 3 sign-off). CoderX provides second-pass technical review.
- Merge target: `main` (the rare case where pillars land directly on main; they precede `dev` work).

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-8133-8167-c85012871391)
