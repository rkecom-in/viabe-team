# VT-727 non-T4 deferral resolution

## Result

`deferral_resolution_delta.jsonl` resolves all 36 `deferred_candidate` records without changing
card expression or touching the 18 T4 corroboration deferrals:

| Route | Source class | Records resolved | Evidence |
|---|---:|---:|---|
| Authoritative effective period | T1 | 25 | Source date text, source locator, canonical/evidence URL, precision and any normalization |
| Originality recheck | T3 | 9 | Seven local mechanical scans and two attributable live-link attestations |
| Vendor-policy validation | T1v | 2 | First-party binding/support locators and explicit effective-date status |

Three T1 records had two deferral reasons, so the complete evidence set contains 12 originality
resolutions: eight mechanical `token-shingle-v1` passes and four attributable live-link
attestations. A record is promoted only when evidence clears every reason attached to it.

The resulting version-3 corpus remains `shadow` / `pending`: 100 records are retrieval-eligible
and 18 T4 records remain research-only pending independent corroboration. O11 ablation and
Fazal-approved graduation thresholds still govern serving admission.

## Safety properties

- The delta contains identifiers and evidence only—no claim, distillation note, claim value or
  raw source expression.
- Corrected immutable version-2 cards are derived from the VT-710 candidates. The claim and
  distillation-note digest must be identical before promotion.
- Every promotion appends an attributable lifecycle event and a complete version-3 corpus
  membership snapshot; no card is updated in place.
- Existing version-1 vectors are copied inside Postgres only after digest equality. The execution
  canary writes to the dev database but performs no Voyage or other network egress.
- Retrieval remains advisory. No evidence row, card, embedding, corpus status or retrieval result
  authorizes a customer send, money movement, consent decision or any other effect.
- No new knowledge source or card was introduced. Official supporting document/page locations are
  recorded only as validation evidence for the already-governed source IDs.

## Execution ownership

The canary is authored but not executed here. CC owns the dev run:

```bash
uv run --no-sync python canaries/vt727_o8_deferral_resolution.py \
  --expected-env dev --tenant-id <REAL_DEV_TENANT_UUID> --execute
```

No schema change or migration is required. The run expects migration 194 and the full-corpus load
and embedding canary to have completed first.
