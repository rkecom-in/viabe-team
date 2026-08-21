# CL-440 — M3 promotion to `main`: evidence of record

**Date:** 2026-08-20 · **Authorization:** Fazal, ~08:25 IST, verbatim **"Push to main."** Relayed by
Clau (0300Z) against the gate (f) raw result. Pillar 7 satisfied.
**Executed by:** CC. **Result:** `main` = `c3c1dd6d` (PR #559). Prod migrations **16 applied / 190
skipped / 0 failed**.

## What the word was given against, and what it actually carried

Fazal's word was given on the gate (f) raw line and the assembled promotion package
(`.viabe/calibration/PROMOTION-PACKAGE-m3-dev-to-prod.md`). **The package framed the payload as M3.
The PR carried 217 commits across three weeks** — `main` was last promoted 2026-07-29 — including 21
migration files, of which 16 were actually pending.

That gap was **corrected on the PR before the merge, not after**. dev→main means all of dev and the
word was for dev→main, so it was not re-asked; but a word given against a one-milestone framing
deserved to meet the real number before the button. Recorded here because the next promotion should
inherit the habit: **state the span, not the milestone name.**

## Gate evidence

| gate | evidence |
|---|---|
| VT-725 (a) per-turn serving | 200 `decision_evidence_links` rows from a deployed turn; **1:1 match** between the new `manager_turn:` decision_ids and the sids in that run's own transcript; tenant alive at query time so an absence would have been visible |
| (b) (c) (d) (e) | closed earlier — per-tenant flip, evidence links, specialist narrowing with 0 out-of-lane against 102 decoys, both forced failures degraded rather than raised |
| (f) full pack | `130 scenarios, 2 findings, 0 domain-floor gaps` — **128/130 = exactly baseline**, against a prediction recorded BEFORE the run |
| (f) no-new-class test | **zero** new classes — both findings non-clean in all three M2 passes, verified against committed summaries |
| M2 blast radius | **R0 = 0** in 390 runs; R1 = 5; R2 = 32 |

**M2 had 20 non-clean scenarios; this build has 2.** The mechanism behind 16 of those 20 — the
Manager answering "I can't build this yet" and never delegating — is gone. The R0=0 composition is
therefore **conservative rather than current**: it was measured on the pre-fix build.

## The finding this promotion turned up

**Migration 208 took its SKIP branch on prod** (prod holds zero knowledge cards, zero corpus
versions). The fresh-database fix made 08-19 for a red pre-push suite was therefore a **latent
prod-promotion blocker**: without it, 208 would have RAISED on prod, and `apply_migrations` breaks on
first failure — every migration from 208 onward would have halted **mid-promotion**, leaving prod
half-applied. It was filed as a local test annoyance.

**Generalisable:** a guard that cannot distinguish "nothing to do" from "the thing I guard against"
fails in whichever environment happens to be empty. That is the same shape as the rc=1 triage
(environment gap wearing a defect's clothes) and the VT-772 harness finding (the instrument destroying
the evidence). Three instances in one week.

## Two guards fired during execution; both were right, neither was bypassed
- **`_SELECTABLE_SET`** caught the newly-approved `lead_winback` becoming agent-selectable. The pin
  grew by exactly one, and `campaign_offer` was additionally named in the must-NOT-be-selectable list.
  `--no-verify` would have shipped a sendable marketing template on the back of a registry commit.
- **`pr-title`** rejected the promotion PR for lacking a numeric VT ref.

## Doc drift corrected in the same pass
CLAUDE.md claimed `"Require status checks to pass"` was OFF (2026-05-30). **It is ON for `main`** —
`ci-success` blocked the #559 merge until CI went green. Corrected in both places it was asserted.

## State at close
Code on `main`; prod schema current; **serving OFF on prod** (`TEAM_KNOWLEDGE_SERVING` unset — the
safe state and also the rollback state); nothing injects into a prompt
(`INJECTS_INTO_PROMPT` is a ClassVar False, asserted by the seam before it returns).

## Open, and explicitly NOT taken by CC
**Prod serving proof needs TWO Fazal calls, not one:** the env-var flip **and** a prod corpus seed —
prod has no cards, so retrieval would return zero candidates even with the flag on. The corpus loads
via `registry_seed`, not migrations, which is precisely why 208 skipped. Both prod-impacting, both
Fazal's under CL-431, neither implied by "push to main". **Raised, not taken.**
