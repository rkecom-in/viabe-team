# Team-Manager Objective — the north-star CC optimizes toward

Owner: Claude Code (implementer). Authored 2026-07-09 on Fazal's ask ("define the objective CC is trying to
achieve"). This supersedes "chase the 45% gate number" as the target. The VT-611 conjunctive gate is a
THERMOMETER, not the objective — see §4.

## 1. The objective (one sentence)
**A small-business owner in India would trust the Team-Manager to run their business operations over WhatsApp
unsupervised** — i.e. it never does something that makes the owner fire it, and it is competent enough to be
worth keeping.

## 2. Acceptance = TWO tiers (this is the real target, per Fazal 2026-07-08)

### Tier 1 — TRUST-BREAKERS = 0 (a COUNT, hard gate)
One occurrence and the owner loses trust; no average smooths it over. A trust-breaker is any of:
1. **Fabrication** — inventing a fact / number / price / business identity / capability not grounded in the
   owner's data or message (invented store name/city/type, made-up pricing, ungrounded ₹ figures).
2. **Wrong or dropped money action** — sending or failing to send a campaign/spend against the owner's actual
   instruction; arming/charging incorrectly; a delegated money task silently never executing.
3. **Loop / stall** — repeating a prior message, question, or link (verbatim OR semantic) with no new
   information; stalling on "I'm on it / I'll update you shortly" without ever delivering the result.
4. **Ignoring the speech-act** — not answering what the owner actually asked (a direct question gets a
   campaign; a correction gets a stall; a count/status ask gets a non-answer).
5. **Promising the impossible** — committing to something the platform cannot do (e.g. "I'll post to your
   Instagram" when it can't; a Zomato/Swiggy action it can't perform).
**Target: 0 scenarios with any trust-breaker. Measured per-transcript, not from an average.**

### Tier 2 — QUALITY ACCEPTANCE ≥ 90%
Of the scenarios with NO trust-breaker, the fraction where the manager's handling is genuinely good — competent,
advancing, right tone + language. **Target ≥90% to ship as "trustworthy to run a business"; 95% = excellent.**
This is deliberately LOOSER than the conjunctive gate: an honest, correct, advancing reply that isn't a
straight-5 is still trustworthy.

## 3. Capability behaviors the objective requires (concrete — "quality" is not vague)
| Behavior | What it means | Measured by |
|---|---|---|
| **Context-aware / never re-ask** | Uses facts already given; never re-asks a stated fact | no re-ask of a fact present in the conversation/profile |
| **Advancing** | Every reply moves to the next concrete step | progression: not a restate/loop |
| **Multi-step execution** | A task yields a real plan/execution, not one canned step | delegated task returns a substantive plan/result |
| **Delegation-and-surfacing** | Delegated work's RESULT reaches the owner | no "I'm on it" → silence |
| **Honest / grounded** | No fabrication; honest "I don't have X" + a next step | honesty; capability-grounding |
| **In-register** | Mirrors the owner's language (Hinglish→Hinglish) | language match |

## 4. Overfitting guard — the 53 eval is the THERMOMETER, not the objective
The objective is **generalization to unseen real owner conversations.** The 53-scenario pack only ESTIMATES it.
Guards, binding on every manager change:
- **Fix the general behavior, never the scenario's exact strings.** No teaching-to-the-test (no special-casing a
  scenario's phrasing to make it pass).
- **The trust-breaker rubric (§2.1) is behavior-general**, not scenario-specific — it applies to any conversation.
- **Hold-out + fresh scenarios**: keep a rotating held-out subset the manager is not tuned against; add new
  real-shaped scenarios periodically; a lift that appears only on the tuned set and not the held-out set is
  overfitting, not progress.
- **Reality check**: if the eval number rises while real conversations don't improve, the metric is being gamed —
  distrust it.

## 5. Relationship to the VT-611 conjunctive gate (the mismatch Fazal flagged)
The VT-611 gate = every dim ≥4 AND mean ≥4.5 (a STRICT conjunctive bar). It is a useful HIGH-BAR internal
thermometer, but it is NOT the acceptance objective and OVERSTATES failure (a 5,5,5,5,4 = mean 4.8 scenario
FAILS it). The acceptance objective is the two-tier bar in §2. Both are reported side-by-side (§ re-score), but
**Tier-1 count=0 + Tier-2 ≥90% is the target going forward.**

## 6. First measurement, both metrics side-by-side (2026-07-09, same 53 transcripts)
Re-scored the SAME 53 gate transcripts (opus per-transcript classification against §2) vs the conjunctive gate:

| Metric | Number | Target |
|---|---|---|
| **Conjunctive gate** (every dim ≥4 AND mean ≥4.5) | **45.3%** (24/53) | — (not the objective) |
| **Tier-1: trust-breaker-free** | **79.2%** (42/53 clean; **11 have a trust-breaker**) | 100% (0 breakers) |
| **Tier-2: quality-acceptable OF clean** | **97.6%** (41/42) | ≥90% ✓ ALREADY MET |
| **Fully acceptable** (clean AND quality) | **77.4%** (41/53) | — |

**Read:** on the RIGHT metric the manager is ~77% acceptable, not 45% — the conjunctive gate nearly halved the
apparent quality. And Tier-2 is ALREADY met (97.6%): **when the manager doesn't trust-break, it's almost always
good.** So the entire gap is the **11 trust-breakers** — a finite, concrete do-or-die list, not a vague "raise the average."

### The 11 trust-breakers (the whole target), by cluster
- **Loop/stall — 7** (ask_owner_resume, efficient_no_overstep, topic_switch_winback, delegation_empty_cohort,
  m_hinglish_winback, bilingual_hinglish, longhaul): the "I'm on it → never delivers" / verbatim-repeat disease.
- **Ignored speech-act — 5** (cross_tenant_friend, efficient_no_overstep, m_fabricated_campaign_sent, longhaul,
  m_hinglish_winback): a direct question/correction got a canned message or a non-answer. (overlaps loop cluster)
- **Fabrication — 2** (hinglish_conversation, longhaul): **INVENTED PRICING** — "free trial", "viabe.in",
  "₹999/month" when asked cost in Hinglish (real = ₹5000/agent, no free trial). A hard trust-breaker.
- **Impossible promise — 1** (gbp_connect_honest_capability): promised a GBP connect walkthrough — GBP is NOT an
  owner-authorizable connect (only shopify + google_sheet are).

### What eliminates them (the concrete path to Tier-1 = 0)
- ~9 loop/stall + ignored → the **emission/progression fix** (VT-629 dispatch rule + the emission rewrite).
- 2 fabrication + 1 impossible-promise → a **capability-grounding rail**: no invented pricing/domain (source
  pricing from config), no promising a connect the platform can't do. Deterministic, count=0.
Two fixes clear the whole list. Delta measured on re-run after each lands (not guessed).
