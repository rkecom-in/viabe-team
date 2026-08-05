# VT-727 full ingestion report

Generated from the tracked VT-710 governed artifacts. Raw source reproductions remain local-only.

- Governed records: **118** (88 distinct local source files; 104 source-governance rows)
- Pipeline input status after authority correction: **100 candidate / 18 research_only**
- Full shadow representatives: **64 validated / 54 deferred**
- Deferred state: **36 candidate / 18 research_only**
- Rejected: **0** (the corpus passed paywall/originality/source-governance hard gates)
- Authority classes: **{'t1': 25, 't1v': 2, 't2': 28, 't3': 45, 't4': 18}**
- Largest source contribution: **5 cards (4.24%)**, below the 10% compilation-review trigger
- Cross-source pairs manually adjudicated after deterministic screening: **68**
- Cross-source retelling groups found/collapsed: **0**
- Independence conclusion: **no cross-source retelling of the same study, case, or thread was found**; related-topic pairs had different decision mechanisms or evidence targets
- Corpus admission: **pending**; O11 and Fazal-approved thresholds remain required
- Effect authority: **false**; retrieval is advisory only

## Overlapping deferral grounds

- `authoritative_effective_date_unverified`: **25**
- `deterministic_shadow_gate_passed`: **64**
- `experiential_claim_requires_independent_corroboration`: **18**
- `originality_attestation_requires_independent_recheck`: **13**
- `vendor_policy_currentness_requires_review`: **2**

Every deferred row in `full_ingestion_disposition.jsonl` carries a concrete route out. A deferral
is not counted as admitted, and no row disappears merely because it is not yet retrieval-eligible.
The three Reddit platform-guidance records are T4, not T1v; only two binding first-party platform
policy records remain T1v. Unknown source licence is retained as provenance metadata but does not
block independently authored knowledge under CL-2026-07-29b.
