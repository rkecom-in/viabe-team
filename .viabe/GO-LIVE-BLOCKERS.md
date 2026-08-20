# GO-LIVE BLOCKERS — reconciled against CODE, 2026-08-10

**Fazal 2026-08-09:** *"Lets list down all thats open and preventing us from going Live."*
**Fazal 2026-08-10:** *"Get CC to close all the blockers."*

**LIVE =** a real Indian SMB owner, not on the allowlist, uses Viabe Team with their real customers
and real money.

---

## Why this file was rewritten — read once, then never again

The first version of this list was **wrong on four of eight items**. B4 (Meta templates) cited a
template deprecated three weeks earlier. B6 (signup gate) was fully built by VT-326. B7 (ownership
verification) was fully built — column, fail-closed gate, prereq registry, API and UI. B8 said
pricing was unset when `plans.yaml` carries live prices. **One cause: I wrote status from documents
and sprint rows instead of from code.** CC caught three of the four; a verification pass caught the
last.

**The fix, applied to every line below: a PROVENANCE TAG.** Nothing enters this file again without
one.

- **`[CODE ✓ date]`** — verified against source this session, citation in the item.
- **`[RUN ✓ date]`** — proven on deployed dev by a real execution.
- **`[UNVERIFIED]`** — believed open, NOT checked against code. **Treat as a hypothesis.**

---

## 🔴 TIER 1 — must close before ANY real tenant

### B1 · VT-738 delegation reliability — **PARTIALLY CLOSED** `[RUN ✓ 2026-08-10]`
Mechanism named and instrumented; re-drive run on deployed dev, forensics captured, 0 tenants lost.
**Measured miss: 2 genuine failures / 33 asserted turns = 6.1%** with **9/9 correct on
no-delegation-expected (zero over-delegation)**. The 33% / 24% / 15.2% figures are **retracted** —
never re-quote them. RV-2 fixed (`e07b5f5d`): `insufficient_data` now asks the owner instead of
looping a cancelled plan.
**Open:** exit gate (c) miss rate <5% ×3 with delegations and honest failures counted **separately**
on a stated denominator; (d) full-pack ×3, no correctness gate weakened. M1 stays open — the
re-drive was powered to name a mechanism, not to confirm a disappearance (P(zero misses in 3 runs)
≈ 0.83 at 6%, so non-reproduction proves nothing).

### B2 · VT-725 O8 retrieval — **BIGGEST GAP, RE-SCOPED** `[CODE ✓ 2026-08-10]`
`grep retrieve_cards_for_turn apps/team-orchestrator/src/` → **no runtime caller.** The row's first
scope item — the retrieval call site at the Manager's turn — **was never built**. Gates (a) and (c)
are *unrunnable*, (f) is vacuous. The "retrieval is live, 20 cards across 6 cases" line was the O11
harness, which does not call that function.
**Bearing on the null result:** it measured whether knowledge helps the ANSWER — that stands. It did
NOT measure the product's retrieval path, because there isn't one.
**Open:** build the call site (shadow, `INJECTS_INTO_PROMPT=False`) → (a) → (c) → (e) forced-failure
degrade → (f). **(e) is the only launch-risk one** — an unproven degrade path means a retrieval
error can take down a live turn.

### B3 · dev→main promotion — **FAZAL'S WORD** `[CODE ✓ 2026-08-10]`
`origin/main` = `62a8b595`; dev is ~10 commits ahead including every safety fix below. **Prod today
can still wedge a tenant permanently, can still accept a stale message as approval, and carries the
effect-blind auto-re-run (B5).** That is the argument FOR promoting, not a footnote.
**Open:** package refreshed and current at the moment the word comes.

### B5 · VT-740 / VT-634 auto-re-run + containment — **HALF CLOSED** `[CODE ✓ 2026-08-10]`
**The hazard:** `orphan_reaper.py:192-203` flips `blocked → planned` when a backoff elapses, reading
status, nullity and time only — **never the send ledger**. Hourly (`scheduled_triggers.py:1500`),
steady state, **and already on `main`**. A workflow that messaged 40 of 100 customers is re-driven
on the same terms as one that sent nothing.
**Closed:** the customer-facing half. VT-740's per-recipient suppression sits on the common send
primitive (`send_whatsapp_template`), which both `campaign/execute.py` and `agents/customer_send.py`
funnel through — so it is path-independent and covers all three re-drive paths without needing the
task→campaign attribution that does not work. Fail-closed on every branch.
**Also fixed** (`9ae4e416`): `prod_workflow_diagnosis`'s effect-state join was **dead**
(`campaign_messages.campaign_id` is never populated), so it would have told a VTR **"SAFE TO CANCEL
— nothing reached a customer"** about a campaign that messaged 40 real people. Now classifies
`unknown` and requires a human.
**Open:** effect-aware wake · `approval_resume.redrive_task` still ungated · `campaign_messages.campaign_id`
populated at write time · withheld tasks must raise `tenant_alerts` **and** be bounded (an unbounded
withhold re-creates the VT-736 wedge) · diagnosis wired to a surface a VTR reads (it is currently
called by nothing).

### B9 · Owner can be left uninformed, twice over — **NEW** `[CODE ✓ 2026-08-10]`
1. `billing/trial_sweep.py:36` — owner notify is a **logging stub** ("logs intent"); `:95,102,135`
   carry `content_sid=None`; and `:367` applies the `trial_expired` transition anyway. **A tenant's
   trial can expire and their phase change to `lapsed` with no message ever delivered.**
2. `main.py:195-198` — `detect_silent_terminal_runs` is **boot-only and inert** because no live code
   writes the `final_outcome` it keys on. The detector for "the run finished and the owner never
   heard" does not effectively run.
Two independent paths to a silent failure on the money path, with no alarm on either.

---

## 🟠 TIER 2 — must close before CHARGING

### B8 · Billing — **RE-STATED; the old text was wrong** `[CODE ✓ 2026-08-10]`
**NOT blockers (built):** Razorpay makes a real `subscription.create` (`razorpay_subscribe.py:154`,
stub replaced) with per-tenant advisory lock, single-use trial token consumed inside the lock,
UNIQUE backstop, and a live/test key-prefix guard that 503s rather than transacting. `plans.yaml`
carries live prices with a fail-closed `offered_tiers` allowlist.
**Genuinely open:**
- **Per-specialist pricing does not exist.** Config is flat per-tenant tiers — a *different model*
  from the ratified one, not a missing number.
- **No auto-convert edge.** `trial_expired → lapsed` is the only elapse path (mig 121 reshaped the
  CHECK around it). The ratified model needs mandate-charge → `paid_active`, fail → dunning → lapsed.
- **Per-agent trial timing breaks the tenant-scoped phase model** — a tenant may hold one specialist
  in trial and another paid; `tenants.phase` cannot express that. **Schema change.**
- **Razorpay credentials unprovisioned** — Fazal's.
- Stale docstring `razorpay_subscribe.py:6` contradicts `:123` inside one file. Fix it; that class
  of line produced four wrong blockers.

### B7 · Ownership verification — **STRUCK, was fully built** `[CODE ✓ 2026-08-10]`
`tenants.ownership_verified` (mig 148) · fail-closed gate `onboarding_gate.py:100-106` (missing row
→ unmet, NULL → unmet, read error → ineligible) · `activation_registry.py:98`
`requires_ownership_verified: bool = True` **defaulting True** · API `ops_vtr_console.py:501`
(atomic, IDOR-gated) · UI `ownership-decision-panel.tsx`.
**Residue, one line of work:** `note`/`evidence` are optional at `:529-530`, so a VTR can verify with
both blank and the audit records only booleans. **Make evidence required.** Governance, not a gate.

### B4 · Meta templates — **CLOSED** `[Fazal-confirmed 2026-08-09/10]`
`team_welcome4` APPROVED UTILITY en/hi/hing; `team_wakeup2` APPROVED UTILITY all three. Prod sender
`+918108084223` (WABA `1166430683220266`) carries the approved set.
**Ruling:** welcome sends **en/hi ONLY** (`CL-2026-08-10-welcome-is-en-hi-only`). The hing welcome
SID stays registered and **deliberately unused** — do not "fix" it.

### B6 · Signup exposure gate — **CLOSED, was built by VT-326** `[CODE ✓ 2026-08-10]`
Dark-by-default 404, OTP bearer proof with phone-match, per-IP throttle, `X-Internal-Secret` closing
the flood at the orchestrator. The old text ("no rate limit, no proof-of-control") was false in
every clause.

---

## ⚪ NOT blockers — deliberately, so nobody re-litigates

- **O8 `active` flip (D3)** — ships SHADOW; the null result *supports* not flipping.
- **§8 learning loop** — `knowledge/learning_loop.py` exists with zero callers. With no measured
  lift, wiring it amplifies a corpus that is not helping. Post-launch.
- **Corpus growth** (Codex SMB corpus, VT-723, fazal-kb) — valuable, off the path.
- **O9 sensing** — held by Fazal's own 1.1/1.2 sequencing.
- **VT-730 mailbox/liveness** — hardening; the wedge fix removed the acute danger.

## ⚠️ The one I will not classify without Fazal — shared sender
**VT-286 (Meta Embedded Signup, owner-owned WABA per tenant)** is the structural answer to
block/spam blast radius: today ALL tenants send from **one number** (`+918108084223`), so one
tenant's customers blocking us degrades deliverability for every other tenant. VT-741 bounds
per-customer frequency; it does **not** bound a tenant's aggregate volume, which is what moves the
Meta quality rating.
**Interim mitigations, none built:** per-tenant quality accounting with auto-throttle · tenant→number
**pinning** (never round-robin) with a **nursery number** for new tenants · aggregate per-tenant
daily cap · capture the block signal as a permanent per-customer hard-stop.
**Fazal's call:** his standing rule is *don't park must-haves post-launch*. With a first cohort of a
few tenants the blast radius is small; at scale it is existential. **Recommend: mitigations now,
VT-286 immediately after the first cohort proves the journey.**

---

## Closure order for CC (dispatched 2026-08-10)
**1.** B5 remainder (live prod hazard) → **2.** VT-741 signals + tiers → **3.** B1 gates (c)+(d) →
**4.** B2 call site + gates → **5.** B9 money-path silence → **6.** B7 evidence-required →
**7.** B3 package current.
**Fazal-only, cannot be closed by CC:** promotion word · price level · Razorpay credentials · prod
env vars · the VT-286 sequencing call.
