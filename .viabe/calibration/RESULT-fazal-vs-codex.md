# RESULT — Fazal vs Codex, 25 India-SMB judgment scenarios

**Scored by Clau 2026-08-11**, on RULE not label, per the protocol fixed before Codex ran.
Fazal answered cold. Codex answered blind by paste, never seeing Fazal's answers or the repo file.

---

## ⚠️ FIRST: WHERE MY PRE-REGISTERED PREDICTION WAS WRONG

I said I would report the failures as prominently as the result. Here they are.

**I predicted divergence on Q01, Q05, Q09, Q22, Q25. I got ONE clean hit out of five.**

| Predicted | What happened |
|---|---|
| **Q01** — "a generic model likely says *don't borrow at 14% on an 8–11% margin*, missing that 2% over 21 days is ~35% annualised. **Fazal's single most likely outperformance.**" | **WRONG.** Codex chose the same option and did the same arithmetic: *"the 2% discount exceeds roughly 0.8% overdraft interest for 21 days."* (14% × 21/365 = 0.805%. Correct.) |
| **Q22** — "likely grants the full raise or refuses; the staged-against-targets structure is less likely." | **WRONG.** Codex chose the staged option, same as Fazal. |
| **Q25** — "likely takes the smaller, prudent bet; Fazal takes the full bet with risk transferred, which is the better answer." | **WRONG.** Codex took the return-clause option first, same as Fazal, and added a fallback. |
| **Q05** — "likely says withhold payment." | **HALF.** It diverged, but not as predicted — it chose written commitment + second supplier, not withholding. |
| **Q09** — "the segmented branch is unlikely to be reproduced." | **CORRECT.** Codex gave the first move with no fallback and no segmentation. |

**The load-bearing error:** my whole thesis was that a generic model would be weak at *computed*
trade-offs and *conditional structures*. **It was not weak at computation** — it did the Q01
arbitrage, the one I had singled out as most likely to show human superiority. My mental model of
where frontier models are weak is out of date, and this instrument existed precisely to catch that.

---

## THE SCORING

| Q | Fazal | Codex | Verdict |
|---|---|---|---|
| Q01 | 2 | 2 | **SAME RULE** |
| Q02 | 6 | 4 | **DIFFERENT RULE** |
| Q03 | 5 | 2+5 | SAME RULE (Codex superset) |
| Q04 | 5 | 5 | **SAME RULE** |
| Q05 | 1 | 5+4 | **DIFFERENT RULE** |
| Q06 | 4+6 | 6+2 | compatible, different tactic |
| Q07 | 4, then 1 | 4, then 2 | compatible-partial (same first rule, opposite fallback) |
| Q08 | 3 | 3 | **SAME RULE** |
| Q09 | 4→6, 1 for a regular | 4 | compatible-partial (no fallback, no branch) |
| Q10 | 4 | 4 | **SAME RULE** |
| Q11 | 3 | 3 | **SAME RULE** |
| Q12 | 1 | 1 | **SAME RULE** (both reason to purpose-limitation) |
| Q13 | 4 | 6 | compatible, different tactic (behavioural vs category targeting) |
| Q14 | 5 | 4 | compatible, different tactic (both: never pay unmeasured) |
| Q15 | 3 | 5+3 | compatible, different tactic (Codex also pauses growth) |
| Q16 | 1 | 1+6 | SAME RULE (Codex superset) |
| Q17 | 3 | 3+4 | SAME RULE (Codex superset) |
| Q18 | 3 | 4 | **DIFFERENT RULE** |
| Q19 | 5 | 5 | **SAME RULE** |
| Q20 | 4 | 4 | **SAME RULE** |
| Q21 | 2 | 2 | **SAME RULE** |
| Q22 | 2 | 2 | **SAME RULE** |
| Q23 | 6 | 6 | **SAME RULE** |
| Q24 | 2 | 2 | **SAME RULE** |
| Q25 | 3 | 3, then 2 | SAME RULE (Codex superset with fallback) |

**Same rule: 16 · Compatible, different tactic or partial: 6 · Different rule: 3.**
**Directional agreement: 22/25 (88%). Strict same-rule: 16/25 (64%).**

### A defect in my own pre-registration
I fixed thresholds (≥20 / ≤15 / 16–19) but **never specified whether "agreement" meant strict or
directional.** Directional gives 22 ⇒ "judgment is not our moat." Strict gives 16 ⇒ "inconclusive."
That ambiguity is mine and it is exactly the kind of post-hoc latitude pre-registration exists to
remove. **Resolving it on the reasoning rather than the arithmetic:** the six "compatible" cases are
cases where Fazal and Codex would take actions that differ in tactic while serving the same
objective — a Manager executing either would not harm a tenant. The three "different rule" cases are
where they would genuinely do opposite things. **The decision-relevant number is 3 genuine
divergences out of 25.**

---

## THE THREE GENUINE DIVERGENCES — and they share a pattern

**Q02 · Tiffin pricing.** Fazal raises to ₹3,400 for everyone and adds visible value, deliberately
repositioning as the premium option against low-margin competitors. Codex creates ₹2,700/₹3,600
tiers so customers self-select. **Fazal refuses to keep a low-end option at all; Codex preserves one.**

**Q05 · The defaulting distributor.** Fazal pays in full — goodwill extended once, on relationship
grounds, contingent on the fix. Codex demands a dated written commitment plus compensation *and*
qualifies a second supplier in parallel. **Fazal spends ₹1.8L of cash on six years of relationship;
Codex converts the relationship into a contract and hedges it.**

**Q18 · Delivery.** Fazal runs an own delivery boy for regulars plus aggregator overflow, explicitly
to preserve direct customer contact. Codex uses the aggregator until a cost threshold justifies
hiring. **Fazal pays fixed cost now to own the customer relationship; Codex optimises cost first.**

**And a fourth signal in the partials — Q07.** Same first move (ask the distributor for credit), but
Fazal's fallback takes the 22% NBFC money because he computed ₹8,438 as nominal against season
profit; Codex refuses to *"finance an uncertain bet at 22%."* **Fazal has higher risk appetite on a
bounded, computed bet.**

### The delta, stated in one sentence
> **Fazal weights relationship continuity and direct customer ownership materially higher than the
> model does, and is more willing to commit capital to a bounded, computed bet. The model is more
> contractual, more hedged, and more cost-optimising.**

Three principles. Not a corpus.

**And the delta is plausibly correct for the domain,** which is the part worth noticing: in Indian
SMB, the relationship *is* the operating system. A six-year distributor and a customer who knows your
delivery boy are not sentimentality; they are the supply chain and the retention mechanism. A model
optimising contractually is applying a US-enterprise prior — which is precisely what the original
825-scenario corpus was made of.

---

## THE DECISION THIS FORCES

**Judgment is ~88% commodity. Do not build a judgment corpus.** The measurement says a generic
frontier model already reaches Fazal's call on 22 of 25 India-SMB situations, including the
computed ones I bet it would fail.

**Three consequences:**
1. **All corpus authoring stays cancelled.** The 825 were AI-generated *and* the exercise they were
   meant to serve is now shown to be low-value. Both reasons independently sufficient.
2. **Encode the delta, not the corpus.** Three or four Manager operating principles —
   relationship-weighting, direct-customer-ownership preference, bounded-risk appetite — reviewed by
   Fazal, are the entire Tier-1 output of this exercise. That is a card or two, not a knowledge base.
3. **The moat is where it always was: execution and tenant outcomes.** If the Manager's *judgment* is
   commodity, then everything defensible sits in doing the work reliably and safely (V1-USABLE E1–E6)
   and in the §12 loop capturing what actually happened to real tenants. Nobody can copy that; anyone
   can copy business advice.

**What this exercise cost and returned:** ~25 questions of Fazal's time, and it killed a workstream
that had already consumed three days and would have consumed weeks. It also produced the India-SMB
half of the held-out eval set as a by-product.

## Honest limits (as written after ONE comparator — superseded below)
N=25. Single comparator, single run — Codex's answers were not sampled for stability, so some of the
16 agreements might not reproduce. The three divergences are the finding most likely to survive
replication because they are *reasoned* differences, not coin-flips. Possible mild kb exposure on
Fazal's side (see ANSWERS §Known Limitation) — which would deflate agreement, so the 88% is if
anything a floor.

---
---

# ARM 3 — ChatGPT, added 2026-08-11. This is the replication, and it sharpens the finding.

Fazal ran the same 25 past a second, independent model. **This is the exact limitation I had flagged
as unaddressed**, and it turns a single-run result into a replicated one.

## ChatGPT vs Fazal — the same distribution, a different composition

**Same rule 16 · compatible/partial 6 · different rule 3.** Numerically identical to Codex.
**But not the same three.**

| | Codex divergences | ChatGPT divergences |
|---|---|---|
| | **Q02** tiering vs premium reposition | **Q02** tiering vs premium reposition |
| | **Q05** contractualise + hedge vs goodwill | **Q05** contractualise + hedge vs goodwill |
| | **Q18** defer hiring vs own rider now | **Q08** split ₹60k business / ₹90k personal vs all-personal |

**Q02 and Q05 replicate across two independent models, in the same direction.** Those are signal.
Q18 and Q08 are single-arm and are model variance.

Notable individual reversals: on **Q13** ChatGPT matches Fazal (broad once, then narrow to engaged)
where Codex diverged; on **Q18** ChatGPT matches Fazal where Codex diverged; on **Q23** ChatGPT
independently supplies the manager-gate that Fazal stated in his rationale and Codex omitted. On
**Q05 and Q06 the two models are answer-for-answer identical to each other.**

## Model vs model — the number that reframes everything

**Codex and ChatGPT agree with each other ~22/25, diverging on Q08, Q13, Q18.**

So the two models agree with each other about as much as either agrees with Fazal. **The residual
disagreement is therefore mostly variance, not a stable human-versus-machine axis** — except where
both models independently move away from Fazal in the same direction. That is Q02 and Q05, and now
it is properly evidenced rather than asserted from one run.

## THE DELTA, RESTATED — and it is better than what I wrote after one arm

After arm 1 I called it *"Fazal weights relationship continuity and direct customer ownership higher."*
With arm 3 in, **all four divergences across both models fit a single, tighter pattern:**

> **Fazal commits to one clean position. The models hedge, split, or preserve optionality.**

- **Q02** — Fazal removes the low-end option entirely and repositions premium. Both models build
  tiers and keep a cheap tier alive.
- **Q05** — Fazal pays in full, goodwill once, on the relationship. Both models convert the
  relationship into a written contract *and* qualify a backup supplier.
- **Q18** (Codex) — Fazal hires the rider now for direct customer contact; Codex defers to a threshold.
- **Q08** (ChatGPT) — Fazal borrows the whole amount personally; ChatGPT splits it to land exactly on
  the ₹1.5L floor.

Relationship-weighting is a *consequence* of this, not the root. The root is **decisiveness**. The
models reliably construct the structure that keeps both doors open; Fazal picks a door.

## THE PRODUCT IMPLICATION — the most useful thing this exercise produced

An LLM Manager will, by default, hand an SMB owner a **hedged recommendation**: two tiers, a written
commitment plus a backup supplier, a threshold to revisit later. For a kirana owner who needs a
decision today, *"here are two options you could consider"* is precisely what they already get from
everyone, for free. **Hedging is a product failure mode, not a safety feature.**

**Design principle, derived from measurement rather than taste:**
> **The Manager must commit to a recommendation. Where it hedges, it must say which door it would
> walk through and why.** Optionality-preserving structures are the model's default and they are the
> thing that makes its advice feel generic.

This is distinct from — and must never weaken — the deterministic effect gates. Commit in the
*reasoning*; the rails stay exactly as unbendable as they are (ARCHITECTURE §0.1.1).

## Revised limits
N=25, now with two independent comparators. Q02/Q05 replicate and are the trustworthy finding.
Neither model was sampled for run-to-run stability, so single-arm divergences (Q08, Q13, Q18) should
not be read as characteristic of either model. Fazal's possible kb exposure still deflates rather
than inflates agreement, so ~88% remains a floor.

**Conclusion unchanged and now better evidenced: judgment is commodity, do not build a judgment
corpus. What is NOT commodity is the willingness to commit — and that is a tuning decision, not a
knowledge base.**

---
---

# ARM 4 — gpt-5.6-luna, the PRODUCTION model, ×3 (scored by Clau 2026-08-14)

Raw: `luna-25-answers.json` (CC, unscored, model confirmed `gpt-5.6-luna`, byte-identical prompt).

## Against the pre-registered prediction (mine, 1300Z brief)
Predicted 16–20 directional, low confidence. **Measured (majority-vote across the 3 runs): ~21/25
directional — top of my range, essentially frontier-grade.** The genuine rule-divergences by
majority are **Q02 and Q05 — the exact two that replicated across Codex and ChatGPT.** Luna even
matches Fazal on Q18, where Codex diverged. On single-sample QUALITY, the commodity finding
extends to our cheap production model.

## BUT the pre-registered instability rule fires — and instability is the finding

*"Self-consistency <3/3 on more than ~5 questions: instability outranks accuracy."* Measured:
**10 of 25 questions are not identical across the three runs.** ~5 are cosmetic (option supersets,
ordering). **~5 are meaningful:**

| Q | The three runs | Why it matters |
|---|---|---|
| **Q01** | **4 → 3 → 2. Three different answers, three different FRAMES** (margin-protection · partial rollout · the overdraft arbitrage). Run 3 does Fazal's arithmetic perfectly (≈₹4,800 net); runs 1–2 never attempt it | The capability exists and surfaces ONE RUN IN THREE |
| **Q12** | run 1: **"accept the data sale if customers opt in"** · runs 2–3: refuse outright | **A consent-posture flip.** One run in three gives materially laxer privacy advice on the same facts |
| Q02 | 3 → 4 → 4 | hold-vs-tier flip |
| Q09 | 4 → 3 → 4 | remedy flips between alterations and keep-advance-take-piece |
| Q25 | 2 → 3+6 → 3+6 | the festival bet flips between the small bet and the full-bet-with-clause |

## THE VERDICT — changes what we fix, not what we believe

1. **Quality: commodity, confirmed at our price point.** By majority vote luna reaches Fazal's call
   ~21/25. Cards-as-knowledge stay dead; the corpus decision is unchanged.
2. **Stability is the real product gap.** An owner asking the same question twice can get different
   advice — and on Q12, different *consent posture*. This is the SAME root as VT-738's M4 (classifier
   variance) and the Q01 frame-lottery: sampling variance on decisive turns, not missing knowledge.
3. **Therefore the fix is STRUCTURAL, not knowledge:** (a) **majority-of-3 self-consistency on
   DECISIVE advisory turns** — the tier policy already prices exactly this class (FAST = "a person
   waiting at a decisive moment"; ×3 on the cheap model is still cheap); (b) **pinned tenant
   directives (VT-725 control plane) as the per-tenant consistency lever** — a directive does not
   flap run-to-run; (c) the decisiveness/commit tuning from Arm 3. Effects were never at risk —
   consent GATES are deterministic (Q12's flip could only ever mis-advise, not mis-send).
4. **VT-725 gate (e) stays the only launch-blocking RAG item.** Nothing here revives knowledge
   injection.

**Prediction ledger, kept honest:** my 16–20 band was WRONG on the top end (measured ~21, frontier
level) — I under-rated the cheap model's single-sample quality for the second time in one week. The
instability call was RIGHT and pre-registered. Q01 as the computed-divergence candidate was
one-third right: it diverged by RANDOMNESS, not by inability.
