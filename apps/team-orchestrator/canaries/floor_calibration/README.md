# VT-725 retrieval-floor calibration

`RetrievalProfile.minimum_score` is a **measured** number, not a chosen one. This directory is how it
was measured and how anyone can re-derive it. Fazal authorized the recalibration on 2026-08-07
("Recalibrate."); Clau's brief required the floor to "remain a measured claim anyone can re-derive."

## The finding that forced it

The old floor was **0.62**. Across 600 (case, card) pairs on the real 100-card dev corpus with real
Voyage embeddings, **the single highest-scoring card scored 0.2867.** The corpus was not
under-performing the bar — it could not reach the bar with a perfect card. The floor was inert by
arithmetic.

## Method

1. **`measure_components.py`** — dumps every score component for every (case, card) pair over all 6
   O11 cases against the live dev corpus. It re-computes the engine's arithmetic (the engine's own
   `retrieve()` applies `top_k <= 20` and the floor, so it cannot show a 100-card distribution), and
   then **cross-checks itself against the real engine** on the 20 cards the engine does return per
   case, asserting every component matches to 1e-12. The dump is provably the engine's numbers.

2. **`label_relevance.py`** — labels each pair relevant/irrelevant **blind**:
   - the labeller sees the case's **agent view only** — never `acceptable_characteristics`, never the
     target answer, never risk flags. Labels steered by the answer key would make the floor a
     measurement of the answer key.
   - it never sees a score, a rank, or a card id, so it cannot agree with the scorer by construction.
   - card order is **shuffled per pass** (fixed seed), 3 independent passes, majority vote.
   - pairwise agreement is reported, not assumed — see "label quality" below.

3. **`choose_floor.py`** — reports AUC first (if the scorer cannot separate the classes, no floor is
   a good floor and that must be said before a number is picked), then sweeps the floor on the
   **development split only**. The **validation split is held out** and never used to pick.
   Precision is measured on cards actually **injected** (floor, then `top_k`) — not on everything
   scored, because injected cards are what reach the Manager.

## Result

Pooled development **AUC = 0.753** (chance = 0.5): the scorer does discriminate.

Figures are given for BOTH measurement runs where they differ — see the Voyage non-determinism note
below. Anything quoted as a single number was identical in both.

| floor | dev precision | val precision (held out) | cases still retrieving |
|---|---|---|---|
| 0.000 | 0.375 | 0.174 | 3/3 dev, 3/3 val — *no floor* |
| 0.245 | 0.471 | — | 3/3 dev |
| **0.250** | **0.533** | **0.500 – 0.600** | **3/3 dev; 2/3 – 3/3 val — CHOSEN** |
| 0.255 | 0.538 | 0.500 | 3/3 dev, 2/3 val |
| 0.265 | 0.800 | — | 2/3 dev (n=5 — not a real number) |
| 0.290 | n/a | n/a | 0/3 — nothing retrieves at all |

**0.250** is the measured knee on dev, where precision jumps 0.471 → 0.533 while every dev case keeps
retrieving. The mandate was to bias the margin toward precision (a false card in the Manager's
context is worse than a miss); going higher buys precision only on samples of n<=5 while silently
zeroing whole cases, which is the failure mode that bias exists to prevent. On data it was never
fitted to, it takes precision from 0.174 to 0.500–0.600.

**One held-out case flaps, and it is worth understanding rather than smoothing over.**
`val-restaurant-festival-capacity`'s single best card scores **0.2505 in one run and 0.2500 in the
next** — it sits exactly on the floor, so that case retrieves 1 card or 0 depending on embedding
noise. It is not evidence the floor is too high: **that card is labelled IRRELEVANT**, which is why
val precision *rises* (0.500 → 0.600) in the run where it drops out. The case's 14 genuinely relevant
cards all score well below the floor, so "restaurant retrieves nothing" is the corpus honestly
reporting it has nothing applicable — a real outcome the harness records as zero rather than
disguising. Lowering the floor to recover this case would buy one false positive and no true ones.

## What this does NOT fix — stated so the number is not read as better than it is

- **Recall at 0.250 is 0.229.** Most relevant cards still never surface. That is the RANKING, not the
  floor. A floor can only remove cards; it cannot promote a relevant card the scorer ranked 40th.
- **9 of 35 dev relevant cards are hard-excluded by applicability before scoring**, so the floor
  never sees them. Two of the six cases lose 4 and 7 relevant cards this way.
- **Specialists are unmeasured.** Their 0.58 was the same unreachable guess, so they now inherit the
  Manager's 0.250. That is a borrowed number, flagged as a gap, not a second measurement.
- **Label quality varies.** Pairwise Jaccard agreement across passes runs 0.50–0.83. The weakest case
  (`val-tailor-price-increase`, 0.50–0.53) is also the weakest AUC (0.599) — consistent with noisy
  labels on that case rather than a scorer failure there, but the two cannot be separated from this
  data alone and it is not being claimed either way.

## Two things measured while validating the calibration itself

**The `entity` renormalization shipped alongside this is a proven no-op on the calibration set.**
VT-725 also made `entity` inapplicable (`None`) when the query names no entities, so the floor would
be invalid if that had moved the scores it was fitted to. Re-measuring all 600 pairs after the
change: **entity became `None` in 0 pairs** — every O11 case supplies an industry/archetype, so the
new branch never fires here. The fit stands. The change is still correct and does fire on real turns
that carry no business profile; it simply cannot be credited with anything measured above.

**Voyage embeddings are not deterministic across calls.** Re-running the identical measurement
produced 400 semantic values differing by up to **0.0019** (and 108 recency values by 6e-06, which is
just the `as_of` clock advancing). At the 0.38 semantic weight that is ~0.0007 of score — enough to
move roughly one card in or out of the injected set near the floor (the 0.235 row shifted 19→18
injected between runs). **The chosen 0.250 row was byte-identical across both runs** (15 injected,
precision 0.533), so the floor is not sitting on that jitter — but any future claim about a single
card's retrieval needs to survive a re-run before it is believed.

## Structural findings the sweep exposed (not fixed here — deliberately)

Component ceilings measured across all 600 pairs:

| component | weight | observed max | max contribution |
|---|---|---|---|
| semantic | 0.38 | 0.5554 | 0.211 |
| lexical | 0.24 | 0.1045 | 0.025 |
| entity | 0.10 | **0.0165** | **0.0017** |
| authority | 0.12 | 1.0 | 0.12 |
| applicability | 0.08 | 1.0 | 0.08 |

`lexical` and `entity` carry **34% of the weight and ~2.6% of the attainable score**. The cause is
Jaccard's union denominator: a ~5-token entity set against a ~100-token card can never exceed ~0.05
however perfect the match. This is a **scaling defect, not inapplicability** — the dimension is
measurable, it is just measured on a scale that cannot reach its own weight.

It is deliberately NOT fixed here. Changing a metric changes ranking, which would invalidate the
very fit above. `card_retrieval.SCORE_WEIGHTS` is now pinned by
`tests/orchestrator/knowledge/test_o8_floor_calibration.py`, so whoever does change it is forced to
re-derive the floor instead of letting it rot silently.

Also measured: **67 of 100 cards declare no applicability dimensions at all** (6 unknowns → 0.10 on
that component) while 19 are explicitly `universal`. Whether those 67 really are universal is a
governance claim about the knowledge, not a scorer question, and remains open.

## Re-running

```bash
set -a && . .viabe/secrets/supabase-dev.env && . .viabe/secrets/voyage.env && set +a
apps/team-orchestrator/.venv/bin/python \
  apps/team-orchestrator/canaries/floor_calibration/measure_components.py <tenant-uuid> /tmp/components.json

set -a && . .viabe/secrets/anthropic.env && set +a
apps/team-orchestrator/.venv/bin/python \
  apps/team-orchestrator/canaries/floor_calibration/label_relevance.py /tmp/components.json /tmp/labels.json claude-opus-5

python3 apps/team-orchestrator/canaries/floor_calibration/choose_floor.py /tmp/components.json \
  apps/team-orchestrator/canaries/floor_calibration/labels.json
```

`labels.json` here is the labelling used for the number in `retrieval_profiles.py`. The component
dump is ~600 KB and fully regenerable, so it is not committed. Re-labelling will not reproduce
`labels.json` byte-for-byte (the labeller is a sampled model); the sweep is stable to that, and
re-running the labeller is the honest way to test whether it is.
