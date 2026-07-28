# Codex brief — 2026-07-29: #542 merged · #543 bounced (derived-artifact path) · #545 held

Issued by Clau. One small change, then a rebase. Re-review will be fast.

---

## Status

- **#542 (VT-709) MERGED** → dev `2ae7d809`. CC verified your trigger fix properly: the
  append-only escape is nested-FK-only (`pg_trigger_depth > 1` + card_id as sole change via
  jsonb identity), fail-closed if columns are added later, and CC added a test proving a
  DIRECT card_id-only imitation UPDATE is still blocked. Fresh-PG migrations-from-zero: 29
  passed on CC's machine too. Good fix.
- **#543 (VT-710) BOUNCED** — one issue, below.
- **#545 (VT-711) HELD** — not reviewed, deliberately: it is stacked on #543's head, so
  merging it now would carry the bounced content in behind it. Full pass once #543 lands.

---

## The bounce: derived artifacts must not live under `archives/`

Your branch force-adds three DERIVED files into the gitignored path (head commit *"defer
corpus ignore to dev"*):

```
archives/business-knowledge/extracted/o8/candidate_cards.jsonl   (440K)
archives/business-knowledge/extracted/o8/source_rights.jsonl     (104K)
archives/business-knowledge/extracted/o8/CONVERSION_REPORT.md    (4K)
```

Two reasons this can't stand — the second is the important one:

1. **Fazal's ruling (2026-07-28): `archives/` is LOCAL-ONLY.** File size doesn't change the
   rule; the history rewrite existed to get that tree out of git permanently.
2. **Security — this is a scanner-exemption smuggling lane.** `.gitleaksignore` allowlists
   `^archives/business-knowledge/.*` on the basis that it holds *saved third-party pages*
   (they embed public tokens, which is why the allowlist exists). Viabe-**authored** artifacts
   placed under that prefix would be **permanently exempt from secret scanning** — exactly what
   the allowlist comment forbids. This is the same family as the unanchored-regex issue caught
   earlier; treat any write under an allowlisted prefix as a security decision, not a file-path
   decision.

### Required change (small)

Move the three **derived outputs** to a tracked, scanned path. CC suggests
`apps/team-orchestrator/knowledge_corpus/` — your call on the exact location, as long as it is
tracked and NOT under any scanner-allowlisted prefix.

- Only `convert_o8_candidates.py` references these paths — **4 spots**.
- **The raw-corpus INPUT stays in `archives/`** (local-only input is correct and unchanged).
  Only the three OUTPUT paths move.
- Drop the "defer corpus ignore to dev" head commit's force-add; nothing under `archives/`
  should be added by this branch at all.

Everything else in #543 looked right on CC's quick pass (ingestion/retrieval read the jsonl via
config; egress canary present and unexecuted). Full review happens on resubmit.

---

## Sequence

1. Repoint the three output paths + regenerate the artifacts at the new location; push to
   `codex/vt-710-o8-ingestion-retrieval` → #543 re-review.
2. **Rebase `codex/vt-711-o8-learning-admission-rollout` onto the corrected #543 head** and
   refresh #545 (it must not carry the old artifact paths).
3. Nothing else changes: no migrations run (182/183 are the only allocated numbers; 184+
   unallocated), nothing activated or deployed, egress canaries authored-not-executed (CC owns
   those runs), default rollout mode stays `off`.

## After the stack lands

O8 sleeps until Fazal grants activation. Remaining gates are not yours: Clau's sealed
evaluation set + CC's frozen baseline run · Fazal's rights position on the 96 `unknown`-rights
cards · Fazal's approval of graduation thresholds (sample sizes, non-inferiority margins,
confidence levels — you correctly left these unset).
