# CODEX BRIEF — complete VT-723 and push. (Paste to Codex AFTER its S2 design checkpoint lands.)

**From Clau, 2026-08-13, under Fazal's directive (verbatim):** *"we should have completed those
first before assigning other tasks to CC or Codex. Prepare to complete them on priority. Once Codex
returns from the task assigned to him, you must get all Codex's work completed and pushed."*

**Standing rule this mints: FINISH-BEFORE-START.** No new Codex assignment while Codex WIP exists.
This brief clears the only known WIP (VT-723). Clau maintains the Codex queue accordingly.

## Order of operations

1. **First: the archive branch you were already instructed to create** (`codex/vt723-wip-archive`,
   pushed) — that instruction stands as the safety copy BEFORE any further work touches the files.
2. **Then finish the S2 design checkpoint** (in flight — do not abandon it mid-stream; that would
   repeat the half-done-work problem this directive exists to end).
3. **Then this brief: complete VT-723 to a PR.**

## What "complete" means — the row as re-scoped 2026-07-29, nothing more

- Reconcile your WIP against `.viabe/sprint/VT-723.md` (re-scoped per CL-2026-07-29b-knowledge-not-source:
  inclusion turns on accuracy + value + originality, not source licence).
- Finish the cards to the row's own quality bar: **provenance per card** (source, date, tier),
  claim_key normalisation, applicability metadata. **A card without provenance does not ship** —
  that discipline is the one thing the whole calibration saga proved non-negotiable.
- **Tier honestly.** Nothing you authored or synthesised may carry a human/Tier-1 label
  (CL-2026-08-11: AI-authored enters at T4 ceiling). Fact cards with primary-source citations carry
  their source's tier. If a card's honest tier makes it retrieval-ineligible, it still lands —
  ineligible, correctly labelled.
- Tests + the ingestion-pipeline run script (CC executes any DB load — you never run migrations or
  touch consoles).
- **PR against `dev`, branch `codex/vt723-complete`.** You never merge; CC reviews at full
  thorough-review depth and lands it.

## Landing context you should know (so the PR states it, honestly)

This corpus lands into **SHADOW**: the retrieval call site is not yet wired (VT-725/S2 lane) and
injection is locked off. So completion is a WIP-hygiene and substrate move — the cards become
durable, reviewed, and correctly tiered, available to the control plane when it wires up. **The PR
description must say this plainly** rather than claiming product impact the measurements do not
support.

## Boundaries (unchanged, all standing)
No merge · no allocators · no migrations run · no consoles/secrets/deploys · no sealed-eval access ·
bogus fixtures only · sends never (this is data work; if anything in the WIP touches a send path,
stop and flag it — it should not).

— Clau
