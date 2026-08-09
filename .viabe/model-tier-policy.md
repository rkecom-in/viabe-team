# Model Service-Tier Policy — Standard / Flex / Fast

**Status: RATIFIED (Fazal, 2026-08-06 — "Lets implement this Model tier policy, we will measure for a few days and see if any change is required.") — MEASUREMENT WINDOW OPEN:** spend-by-tier per tenant on the VT-733 console is the review evidence; Fazal revisits after a few days of measured mix. Once confirmed, this file is the single
source of truth; VT-735 implements the mapping FROM this file, and any change here is a
Fazal-visible diff, never a code-side drift.

## The rule (one sentence)

> **We pay for latency exactly where a waiting human feels it: nobody waiting → FLEX (½×);
> a person waiting → STANDARD (1×); a person waiting at a decisive moment → FAST (2×).**

## The three classes

### FLEX — ½ price, delayed. "Nobody is waiting."
Everything the owner never sees happen: the daily 7-day-plan revision pass · memory
distillation · knowledge-graph population and backfill · §7C impact judgments · monthly report
generation · eval/response-bundle generation · entity re-enrichment · any scheduled or
queue-drained job.
*Guardrail:* a Flex call that hits capacity-unavailable retries once, then **falls through to
Standard** — a background job quietly paying full price beats one failing. It never escalates
to Fast.

### STANDARD — 1× price. "A person is waiting, the moment is ordinary."
Every owner-facing conversational turn by default: chat, questions, onboarding steps, status
asks, drafts being composed for review. This is the bulk of the interactive experience at the
normal price — value for money by default.

### FAST — 2× price. "A person is waiting AND the seconds carry risk or money."
A deliberately TINY allow-list, because volume here is low but stakes are highest:
1. **Approval resolution** — the owner just said yes/no to a money action. Every second of
   delay here is the window in which VT-734's duplicate-request race lives; paying 2× on this
   turn is partly a SAFETY spend, not a comfort spend.
2. **Opt-out / STOP processing** — compliance clock; the freeze must feel instant.
3. *(Future, when the revenue agent goes live)* first response to an active buying customer —
   the one place external revenue is waiting on our latency.
*Guardrails:* Fast is allow-listed by call site (never a default anything can inherit), and
carries an internal per-tenant daily Fast-budget; exceeding it degrades to **Standard** (never
to Flex — a decisive moment never gets the slow tier) and flags the tenant on the VTR console,
because a tenant burning Fast budget is a tenant with a runaway loop, not a billing event.

## Why this is the balanced point (illustrative — measured mix comes from VT-733)
Background work dominates token volume (plans, judging, distillation run for every tenant every
day; approvals are rare). At a plausible mix — ~65% of tokens Flex, ~30% Standard, ~5% Fast —
the blended rate is **≈ 25–30% BELOW paying Standard for everything**, while the owner's
experience is strictly BETTER than all-Standard (decisive moments are faster). Cheaper than
today AND better than today is the confirmation this rule is on the right side of both goals.
The real mix lands on the VT-733 cost console as spend-by-tier per tenant, so this claim is
checked against measurement, not left as an argument.

## Standing constraints
- **Deterministic mapping by call-class, in one config surface** — the Manager does not choose
  tiers per-turn. (The future budget-aware Manager may downgrade its own BACKGROUND work when
  near a tenant cap; it may never downgrade the Fast class — economizing never touches safety.)
- **Judge/eval SCORING never runs Flex** — a gate that flakes on capacity is a gate nobody
  trusts. (Bundle *generation* may Flex; scoring may not.)
- Env sovereignty per VT-732: `TEAM_GPT_FLEX=off|background|all` + the Fast allow-list are
  env/config-controlled on Railway, boot-conformance-printed, ledger-recorded
  (`service_tier` per call), priced per-tier in the registry (verified sheet, 2026-08-06).
- **Batch API** (same ½ price as Flex, no capacity-retry class) is the preferred lane for fully
  offline bulk work (eval bundles, nightly rollups) when volume justifies the plumbing —
  VT-733-C decision, not this policy's.

*Proposed by Clau and RATIFIED by Fazal 2026-08-06. VT-735 implements from this file. Changes to this file are Fazal-visible diffs — never code-side drift.*
