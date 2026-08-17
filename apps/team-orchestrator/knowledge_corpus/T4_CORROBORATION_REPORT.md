# VT-723 T4 corroboration report

- Exact forum claims reviewed: **18**
- New governed source records: **33**, representing **32 independent clusters**
- VT-710 pipeline results: **33 inert candidates**, all embedding-deferred
- Earned card-tier mix after archived-byte verification: **2 T1 / 0 T1v / 4 T2 / 0 T3 / 27 T4**
- **Source verification: `T4_CORROBORATION_VERIFICATION.md` + `t4_corroboration_verification.jsonl`.** Every card was checked against the bytes of the source it cites. Six faithful citations retain the class their evidence earns; the other 27 are explicitly demoted to T4 judgment. `assert_corpus_verified` accepts only those two exact postures and has no waiver path. The three unsound promotions are recorded before and after in `t4_corroboration_unsound_promotions.json`
- Authorship authority: **seed** for all Codex distillations; none labelled owner, VTR, or verified outcome
- Claim identity: subject inherited from the target claim, **predicate derived from each card's OWN claim** (it used to be inherited, which made a cited fact carry an invented behavioural instruction); no universal-by-default cards
- Byte binding: each card reaches its source bytes as card -> `provenance.source_ids[0]` -> `knowledge_sources.content_hash`, which is the sha256 of the acquired archive file. `source_content_hash` is the hash of our own extraction input and binds nothing about the source
- Source verifiability: archives are **local-only and gitignored**, so byte verification and deterministic regeneration run only where the archive is present; both are asserted by skip-guarded tests rather than claimed here
- Evidence-state result: **0 candidate / 0 disputed / 18 research_only**
- Semantic retellings counted as corroboration: **0**
- Paywall circumvention: **0**; paywalled candidates were skipped and logged
- Raw source reproductions committed: **0**; archive inputs remain local-only
- Retrieval eligibility granted: **0**
- Effect authority granted: **0**

## Landing posture

This corpus lands in **SHADOW**. The retrieval call site is not wired and prompt injection remains
locked off. These cards and evidence transitions become durable reviewable substrate only; this
change makes no claim of current product impact.

All 18 forum claims remain research-only. The corrected evidence set supplies at most one
qualifying independent cluster to any target. Non-qualifying partial and refuting evidence remains
recorded for audit, but it cannot promote or dispute a parent claim. This is recorded absence, not
silent rejection.
