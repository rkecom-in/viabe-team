# Codex brief — 2026-07-29b: the rights gate is RESET (Fazal ruling, supersedes the earlier position)

Issued by Clau. **This supersedes CL-2026-07-29-card-rights-licensed-sources.** New standing
ruling: `CL-2026-07-29b-knowledge-not-source`.

## What changed and why

Fazal corrected my over-cautious position: *"how would license matter for the 96 unlicensed
cards? We are only using their information as a knowledge, and our criteria of inclusion and
exclusion must be the accuracy and value of the knowledge provided by the card and not its
source."*

He is right. **Copyright protects EXPRESSION, not facts or ideas.** Your cards are original
authored structure and sentences (situation / decision pressure / mistake-or-risk / recommended
action / evidence needed) stating extracted findings — none of the source's expression
survives. Treating those as encumbered because the source page lacked a licence grant confused
the *source* with the *claim*.

Your instinct to record rights and refuse to treat public accessibility as a licence was
**correct discipline** — it just attached to the wrong gate. The record stays; the block goes.

## The new rule

1. **INCLUSION = accuracy + value + measured impact (§6).** Never source licence.
2. **`usage_rights: unknown` no longer blocks embedding or retrieval eligibility.** The 96
   cards are eligible. Lift the `rights_blocked` embedding state for them.
3. **Provenance and source-class STAY** — for their legitimate purpose: **authority weighting**
   in retrieval ranking (§5.3) and conflict resolution (§7.2). A T1/T2 source still outranks a
   T4 one; that is about evidential strength, not paperwork.

## The check that REPLACES the rights gate

**Expression originality.** No card may contain verbatim or near-verbatim source text — it must
state the claim in our own words. Reject or rewrite otherwise. Add this as a pipeline check
(and a test) in place of the rights block.

Three narrow flags to keep (flag, don't block — they're judgment calls, not automation):
- **Compilation/database-rights concentration** — if one source or dataset would supply a
  substantial portion of the corpus, surface it.
- **Contractual ToS** that prohibits extraction — binds by contract, not copyright.
- **Paywalled material obtained by circumventing access** — excluded outright.

## Unchanged (and now correctly justified)

- **Raw archived source pages stay local-only and are never retrieval-eligible.** Those ARE
  reproductions — that rule was right, for this reason rather than the licence reason.
- The 5 `live_link_only` cards keep the honesty convention (never claim the local synthesis is
  the source original).
- Admission still governed by §6 — eligible ≠ validated. Nothing is marked improved without the
  baseline.

## VT-723 re-scoped
From "replace unknown-rights cards" → **"grow coverage from high-authority sources where O11
shows weak slices"**. Additive, not remedial. High-authority sources are preferred because they
earn a stronger tier and better ranking — not because of licensing.

## Also carried (from CC): the O8 retrieval latency test asserts a 100ms wall-clock bound and
sees ~410ms on loaded shared CI runners — green locally, red forever on CI. Make it
perf-advisory or loosen the bound; wall-clock assertions on shared runners are a permanent
flake source.
