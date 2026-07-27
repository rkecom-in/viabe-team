# Codex Working Brief — separate clone, separate branches, zero collision with CC

**Audience: Codex (external builder). Standing (Fazal 2026-07-22).** This brief defines HOW
you work; WHAT you work on stays in the normal task substrate (`.viabe/sprint/VT-*.md`).
Governance: Clau = architecture + audit-after seat (the Cowork seat, renamed 2026-07-22);
CC = implementer + your reviewer/merger; ratification and grants are always Fazal.

## 1. Your workspace — a SEPARATE clone, never the shared tree

CC, Clau, and Fazal share ONE working tree at `/Users/fazalkhan/development/viabe-team`
(single git index — CL-418). You never touch it. Your workspace:

```bash
cd /Users/fazalkhan/development
git clone git@github.com:rkecom-in/viabe-team.git viabe-team-codex
cd viabe-team-codex
bash scripts/install-hooks.sh        # the pre-push hook is the safety gate
cd apps/team-orchestrator && uv sync # Python 3.13 env
```

All your reads AND writes happen in `viabe-team-codex/`. If you ever find yourself editing
a path under plain `viabe-team/`, stop — wrong folder.

## 2. Reading tasks

- Tasks are VT rows: `.viabe/sprint/VT-<N>.md` in YOUR clone after `git fetch origin dev` +
  fast-forward. Rows publish to `origin/dev` via CC's pushes — if a row Fazal mentioned isn't
  visible yet, fetch again later or ask; do NOT guess its content.
- Your current rows: **VT-705** (O11 harness — read its sealed-set custody rule), **VT-706**
  (contract extensions, draft), **VT-707** (ingestion pipeline). Specs of record:
  `.viabe/o8-knowledge-engine-design.md` + `docs/agent-framework/ARCHITECTURE.md` +
  `EXTERNAL-BUILDER-ONBOARDING.md`.

## 3. Branch + PR discipline

- Always branch from fresh `origin/dev`: `git fetch origin && git checkout -b
  codex/vt-<N>-<slug> origin/dev`. One VT row = one branch = one coherent PR.
- Re-sync daily: rebase your branch on `origin/dev` before pushing (CC lands continuously;
  stale branches rot fast).
- Push ONLY `codex/*` branches. **Never push to `dev` or `main`. Never merge anything —
  including your own PRs.** PRs target `dev`; CC reviews and merges. Address review findings
  on the same branch.
- PR description = your report: what/why, spec sections implemented, tests run + results,
  anything deferred, anything that surprised you. This is your signal channel — the
  `.running/` signal pipeline is working-tree-local to the main folder and is NOT yours.

## 4. Allocators — HANDS OFF

- **Never run `scripts/vt_id_allocate.py` or `scripts/migration_id_allocate.py`.** The
  counters live in the tracked tree; two clones racing them = collisions. Clau allocates all
  VT-IDs and migration numbers and stamps them into your row. Need one? Ask in the PR or via
  Fazal.

## 5. Hard boundaries (unchanged from your onboarding, restated)

- No secrets, env values, consoles, or deployed-environment mutations. Env questions →
  names→booleans only, via CC.
- Canaries needing real network egress or deployed-dev: build them runnable, then FLAG — CC
  executes and reports. Fail-not-skip semantics still yours to encode.
- **Sealed held-out set (VT-705): you must never read, author, or infer it.** You deliver the
  scenario spec + dev/validation sets; CC authors and custodies the sealed set. If sealed
  content ever appears in your context, say so in the PR immediately.
- No live routing, no gate changes, no effect paths — your current rows are contracts,
  harness, and quarantined ingestion by design. A task that seems to need more: stop, flag.
- Tests: `uv run pytest` scoped to what you touched + the dep-less smoke must pass locally;
  the pre-push hook must be green before any push (no `--no-verify`).

## 6. Merge conflicts and collisions

Your separation from CC is by FOLDER (clone) + BRANCH (codex/*) + FILE OWNERSHIP (your rows
touch knowledge/, agent_framework contracts, canaries/o11/ — CC's launch work rarely
overlaps). If a rebase surfaces a real conflict with CC's landed work, resolve conservatively
(their side wins on anything you don't own) and note it in the PR; if the conflict is
architectural, stop and flag to Fazal/Clau rather than resolving silently.
