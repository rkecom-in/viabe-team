# GO-LIVE BLOCKERS — the complete list, ranked (Clau, 2026-08-09)

**Fazal 2026-08-09:** *"Lets list down all thats open and preventing us from going Live."*

**Definition used:** LIVE = a real Indian SMB owner, not on the allowlist, uses Viabe Team with
their real customers and real money. Anything that must be true before that is a blocker.
Everything else is post-launch work, however valuable.

**Honest headline (REVISED 2026-08-09 after Fazal re-shared the welcome SIDs): 8 blockers, and
B4 was MINE-and-wrong — the Meta approvals were already done. So it is now 7 real blockers.
3 are Fazal-only (promotion word · price level · ownership process). 4 are CC. Codex closes
NONE of them — its work is valuable but not on the critical path.**

**Consequence of the B4 correction:** the earliest-LIVE date is **no longer gated by the Meta
console**. It is gated by engineering — B1 delegation reliability and B3 the promotion word.
That is a better position than this file claimed yesterday, and it was true yesterday too.

---

## 🔴 TIER 1 — MUST close before any real tenant (5)

### B1. Delegation reliability (VT-738) — **CC, root cause NAMED 2026-08-09/10**
**The "~1 task in 3" figure was wrong and is retracted — do not re-quote it.** It came from
"34 delegating turns vs 11 honest-failure replies", two counts with no shared denominator (and
11/45 is 24%, not a third, so it was never even arithmetically a third). On the pack's own
denominator — steps asserting `expect_sr_delegation: true` — the measured number is
**3 misses / 33, of which 2 are genuine delegation failures = 6.1%**, with **9/9 correct on
`expect_sr_delegation: false` (zero over-delegation)**. CC re-derived every load-bearing number
from `reports/vt734_critical_x3.json` and discarded nine investigator claims as unsafe.

**Four mechanisms named with file:line** (full chain in `.viabe/sprint/VT-738.md`):
**M1** `manager/workflow.py:342` — a dispatch that spawns nothing never reaches `manager_review`,
so `or "escalate"` fires silently, writing no audit row and no incident. **M2**
`canaries/convo_harness.py:515-526` — the route marker is a `campaigns`-row-existence proxy read
before an async write, so a slow-but-correct delegation reads as `none`; the quoted
`['none','sales_recovery','none']` array is partly this artefact. **M3** `task_store.py:44` keeps
`blocked` in `TASK_ACTIVE`, holding the tenant's slot, which skips D3 + the LLM route and hands
the turn to the legacy sync brain. **M4** `campaign_first_contact.py:74-84` requires VERB ∧ NOUN,
so Hinglish phrasings ("…taiyar karo", "…offer draft kar do") fall out of the deterministic router
to an unstable LLM classifier — **both genuine misses are Hinglish**, and that is the whole of the
non-determinism.

**Why it still blocks:** M4 lands on our target persona's own language, and M1 is silent by
construction. **Why it is smaller than I said:** 6.1%, not 33% — and CC's own correction, not mine.

### B2. VT-725 — **RE-SCOPED 2026-08-10. It was never "4 gates from closed."**

**CC's finding, and it corrects both of us:**
```
grep -rn "retrieve_cards_for_turn" apps/team-orchestrator/src/ --include="*.py"   # no output
```
**The row's FIRST scope item — the retrieval call site at the Manager's turn — was never built.**
The only caller in the repo is VT-725's own canary. The original audit line in the row ("grep
across `agent_graph/` returns ZERO references") is still true today, six days after the row was
written to close exactly that.

**Consequences, stated plainly:**
- Gates (a) and (c) are **unrunnable, not unrun.** There is no product turn to trace and no
  decision to link evidence to.
- Gate (f) is **vacuous** — nothing is wired, so nothing can regress.
- The status line "retrieval is live: 100 cards, 20 injected across 6 O11 cases" was the **O11
  harness**, which does not call that function. CC wrote it, CC retracted it.
- **Bearing on the null result:** the treatment measured whether knowledge helps the *answer* —
  that survives. It did **not** measure the product's retrieval path, because there isn't one.
- **Bearing on what I told Fazal about the RAG:** I said the retrieval half was "built and proven."
  Built and canary-proven, yes. **Wired into the Manager, no.** That distinction is the whole
  difference between a moat and a library, and I stated it too generously.

**Real remaining scope:** build the call site (shadow, no injection) → then (a) and (c) become
runnable → (e) forced-failure degrade → (f) full-pack ×3.
**Sequencing (CC's call, accepted):** the call site lands AFTER the VT-738 re-drive. Adding DB
reads and latency to the Manager's turn while VT-738 is diagnosing turn-level behaviour would
confound the measurement.
**Why it blocks:** (e) — an unproven degrade path means a retrieval error can take down a live turn.

### B3. dev→main promotion — **Fazal's word, package from CC**
45+ commits on dev; prod is 12 days stale and does NOT have: the single emission choke's
successors, the approval-ordering invariant (VT-734), the wedge fix (VT-736), model governance
(VT-732). **Prod today can still wedge a tenant permanently and can still accept a stale message
as approval.** Migration gap: 188 + 189 (+ 190/194/196/197/198 since). All additive; prod O8
tables verified empty, so 189 is safe.

### B4. ~~Meta template approvals~~ — **CLOSED on the Meta side 2026-08-09. Small CC follow-up.**

**I wrote this blocker wrong and it stayed wrong for a day.** My text said "welcome template
declined as MARKETING; hi-Latn variants unregistered." Both halves were stale: the MARKETING
force-conversion hit **`team_welcome3`**, which was DEPRECATED on 2026-07-02 and replaced by
`team_welcome4`; and the hi-Latn (`hing`) variant has been **registered in
`twilio_templates.yaml` since 2026-07-18**. I carried a three-week-old fact forward without
reconciling it against the registry — a Rule-14 miss on the one blocker I told Fazal was the
critical path. The earliest-LIVE-date claim at the bottom of this file rested on it.

**Ground truth (verified 2026-08-09):** `team_welcome4` is **Meta-APPROVED UTILITY in all three
languages** — en + hi by a real Content-API read on 2026-07-02, hing by Fazal's confirmation
2026-08-09. All three SIDs Fazal re-shared match `twilio_templates.yaml` and `.viabe/templates.md`
byte-for-byte. Prod's sender (`+918108084223`, WABA `1166430683220266`) already carries the
approved set, so prod template sends work day-one.

**What actually remains (CC, small — NOT Fazal, NOT a Tier-1 blocker):**
1. **Flip the hinglish template register.** `owner_locale.template_register()` returns `'en'` for
   a hinglish owner, gated on exactly the approval Fazal has now given. A hinglish-preference
   owner currently receives the ENGLISH welcome. Guarded per-template resolution (absent hing
   variant → `en`), because only welcome4 + wakeup2 have one.
2. ~~Verify `team_wakeup2`'s category~~ — **Fazal-confirmed UTILITY 2026-08-09.** The
   force-conversion worry (wakeup v1 went UTILITY→MARKETING after approval) is answered. Keep the
   real Content-API read as a cheap batch confirmation alongside the welcome4 SIDs, but it is
   **evidence-of-record, not a gate** — nothing waits on it.
3. **Dev sender WABA** (`+18704122234`, US) may not carry the approvals — dev-only, tolerable per
   the whitelist ruling (dev is mostly free-form session sends). Not a launch blocker.

### B5. VT-634 prod failed-workflow handling — **CC, rostered, launch-blocker (Fazal 2026-07-10)**
Contain without re-running, diagnose separately, surface on the VTR console. Design landed; build
did not. **Why it blocks:** the wedge fix handles the dev-side symptom; prod still has no operator
surface for a failed workflow on a real tenant's money path.

---

## 🟠 TIER 2 — MUST close before CHARGING (3)

### B6. Signup exposure gate — ~~CC~~ **ALREADY CLOSED (VT-326). CC verified 2026-08-10.**
The blocker text said *"`/api/signup` is pre-auth with no rate limit and no proof-of-control."*
**Every clause of that is false today** — VT-326 shipped the whole gate. Verified in
`apps/team-web/app/api/team/signup/route.ts`, not inferred from a comment:

- **Dark by default** — `ENABLE_PUBLIC_SIGNUP !== 'true'` → 404 (`:21`). The route's own comment
  makes the point: *"A comment is not a gate; this is."*
- **OTP-before-create** — a `verifyVerifiedNumberToken` bearer proof is required (`:34-42`), and the
  proof must match the number being signed up, so a token for phone A cannot create a tenant for
  phone B (`:47`, `phone_mismatch`).
- **Per-IP throttle** — `checkSignupRateLimit(trustedClientIp(request))` → 429 (`:53`).
- **Not just an edge gate** — `X-Internal-Secret` on the forward, so only team-web can reach the
  orchestrator's BYPASSRLS create; flooding is closed at the source too (`:66`).
- **Tested** — `tests/api/signup-gate.test.ts`, **8/8 passing**.

**This is the third stale entry found in this list in one night** (B4 was wrong on both halves; the
L3 hinglish-flip premise was wrong on three). The list's headline count is therefore not reliable
as a go-live gate until every remaining entry is reconciled against the code the way B4 and B6 now
have been — Rule 14 applied to the document that is supposed to be driving the launch decision.

### B7. Ownership verification (VTR gate) — **process + CC**
`ownership_verified` defaults false and is a non-bypassable execute gate (Fazal 2026-07-01), but
the VTR flip procedure is not operationally defined. **Why it blocks:** we would be sending on
behalf of a business nobody confirmed the owner owns.

### B8. Billing live — **Fazal + CC**
Pricing STRUCTURE ratified (Manager free · flat per-specialist · per-agent free month + UPI
mandate). **LEVEL is unset** and waits on VT-733-C measured cost. Razorpay/UPI mandate flow at
activation is not built. **Why it blocks charging:** no price, no mandate, no invoice.

---

## ⚪ NOT blockers (deliberately, so nobody re-litigates them)
- **O8 activation (D3)** — the RAG ships SHADOW; the null result *supports* not flipping. Not a
  blocker; a post-launch experiment.
- **Corpus growth** (Codex's SMB corpus, VT-723, fazal-kb ingestion) — valuable, not on the path.
- **O9 sensing** — held by Fazal's own sequencing; a launch without it is still a product.
- **VT-730 mailbox/liveness** — hardening; the wedge fix removed the acute danger.
- **VT-735 flex, VT-733 A/B** — cost work; affects margin, not go-live.

---

## The overnight plan (what CAN actually be closed while Fazal sleeps)
**CC:** B1 (delegation root-cause + fix) → B2 (VT-725 four gates) → B3 package ready for the word
→ **B4 tail (hinglish register flip + wakeup2 category read — small, fold in wherever it fits)**
→ B6 (signup gate) if time. **Codex:** SMB corpus (not a blocker — parallel, no dev contention).
**Fazal, and only Fazal:** B3 promotion word · B8 price level · B7 ownership procedure.

**Realistic:** B1+B2+B3-package overnight is achievable. With B4 corrected, **nothing on the
critical path is waiting on a third party** — the earliest credible LIVE date is now set by
delegation reliability and Fazal's promotion word, both of which are inside our control.
