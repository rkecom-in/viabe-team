# VT-723 T4 corroboration report

- Exact forum claims reviewed: **18**
- New governed source clusters: **33**
- VT-710 pipeline results: **33 inert candidates**, all embedding-deferred
- Source-tier mix: **7 T1 / 1 T1v / 17 T2 / 8 T3**
- Authorship authority: **seed** for all Codex distillations; none labelled owner, VTR, or verified outcome
- Claim identity: subject inherited from the target claim, **predicate derived from each card's OWN claim** (it used to be inherited, which made a cited fact carry an invented behavioural instruction); no universal-by-default cards
- Byte binding: each card reaches its source bytes as card -> `provenance.source_ids[0]` -> `knowledge_sources.content_hash`, which is the sha256 of the acquired archive file. `source_content_hash` is the hash of our own extraction input and binds nothing about the source
- Source verifiability: archives are **local-only and gitignored**, so byte verification and deterministic regeneration run only where the archive is present; both are asserted by skip-guarded tests rather than claimed here
- Evidence-state result: **15 candidate / 1 disputed / 2 research_only**
- Semantic retellings counted as corroboration: **0**
- Paywall circumvention: **0**; paywalled candidates were skipped and logged
- Raw source reproductions committed: **0**; archive inputs remain local-only
- Retrieval eligibility granted: **0**
- Effect authority granted: **0**

## Landing posture

This corpus lands in **SHADOW**. The retrieval call site is not wired and prompt injection remains
locked off. These cards and evidence transitions become durable reviewable substrate only; this
change makes no claim of current product impact.

bk028-comment-sample-loop-for-service-demand is disputed: a SaaS field experiment supports
carefully targeted free trials, while an independent randomized study found free distribution can
reduce later paid demand. bk025-high-trust-b2b-free-diagnostic-to-paid-pilot and
bk113-good-glamm-integration-capacity remain research-only because only partial evidence was found
for their exact formulations. This is recorded absence, not silent rejection.
