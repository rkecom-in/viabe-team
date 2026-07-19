---
id: VT-477
title: "Onboarding confirm-yes stall — DIAGNOSIS (plan-first, read-only investigation)"
status: Plan-ready
priority: Critical
type: BUILD
area: team-orchestrator
investigated: 2026-06-28
investigator: CC (read-only — code + live dev DB, Rule #14 grounded)
supersedes_mechanism: "onboarding-journey-defects-and-richmedia-plan.md §Defect 2 (the redelivery/idempotency hypothesis is DISPROVEN by the data)"
---

# VT-477 — the onboarding confirm-"yes" stall: diagnosis + plan

## TL;DR

**There is NO confirm-advance bug. The "cursor stuck at 1" reading was a MISREAD of a
normal cursor 0→1 advance.** The "yes" the owner sent at 12:44 was a "yes" to the
**CATEGORY** confirm (cursor 0), which it correctly recorded (`category=...`) and advanced
0→1. The **CITY** confirm ("And you're based in Mumbai — correct?") is at cursor 1 and is
**legitimately pending** — the owner has not yet replied to it. There has been **no inbound
at all since 12:44:26**, so nothing "failed to advance city": city was never answered.

The redelivery/idempotency hypothesis in the batch plan (§Defect 2) is **disproven by the
live data**: there were **zero Twilio redeliveries** in the window — six distinct inbounds,
six distinct MessageSids, all `dupe_status=false`. The `journey.py:155` idempotency branch
**never fired** for this tenant.

The 152b24d greeting fix is **working correctly** — that is precisely why `category` holds
the real GBP value and not `"Hi"`.

---

## 1. DIAGNOSIS — grounded in code + live dev DB (tenant 63211ce5-8074-4960-b409-a57c69fe5356)

### 1a. The live journey state (read-only, dev Supabase)

```
onboarding_journey: status=active  cursor=1  qlen=9
  answers = {"category": "Telecommunications service provider"}
  skipped = []
  last_message_sid = SM5ed115864de44fe8aa5824bae603c62a
  updated_at = 2026-06-28 12:44:31.81+00   completed_at = NULL

question_queue (cursor index → field/kind):
  0  category         confirm   "We found you're a Telecommunications service provider —"  draft="Telecommunications service provider"
  1  city             confirm   "And you're based in Mumbai — correct?"                    draft="Mumbai"   ← CURSOR IS HERE
  2  about            confirm   "Here's how we'd describe your business: ..."
  3..8  operating_hours / product_categories / delivery_available / payment_methods / peak_shopping_days / inventory_turnover  (gap)
```

`_advance` (journey.py:211) sets `cursor`, `answers`, **and** `last_message_sid` in one UPDATE.
So `cursor=1` + `answers={category}` + `last_message_sid=SM5ed115` is exactly the fingerprint
of: **"a reply applied at cursor 0 (the category confirm) → recorded category → advanced to
cursor 1."** It is NOT the fingerprint of a stall at city.

### 1b. The message flow, reconstructed from `episodic_events` (`owner_message_received`) — NOT redacted

The batch plan said the bodies are redacted and the exact step "needs un-redacted local repro
or a logfire trace." It does not — `episodic_events.payload.body_length` is recorded per inbound
and is enough to reconstruct the flow:

| time (UTC) | MessageSid | dupe_status | body_length | run status |
|---|---|---|---|---|
| 10:24:59 | SM20d29ea9… | false | 2 | **running (stuck)** |
| 10:31:42 | SM34cde8a7… | false | 2 | completed |
| 11:05:30 | SMc7b362c0… | false | 2 | completed |
| 11:47:20 | SM1bf2a8e5… | false | 2 | completed |
| 12:42:39 | SM0a7a726e… | false | 2 | completed |
| **12:44:26** | **SM5ed11586…** | false | **3** | completed |

- **Six distinct MessageSids, all `dupe_status=false`.** Every one is a genuine new inbound,
  not a redelivery. (A redelivery would carry the SAME Sid and be `dupe_status=true` — and would
  be killed at the journey gate, runner.py:715 `not event.dupe_status`. None were.)
- **Five body_length=2 inbounds** (e.g. "hi" / "ok" / "हा") landed on the **category** confirm
  (cursor 0) and did **not** advance the cursor — consistent with the 152b24d greeting / bare-token
  re-present path (`_greet_then_question`, journey.py:178-179) which returns WITHOUT calling
  `_advance` and WITHOUT touching `last_message_sid`. That is why `last_message_sid` was never any
  of those five SIDs.
- **The one body_length=3 inbound** at 12:44 (SM5ed115, "yes") is the only one that took the
  confirm branch (journey.py:184-189): `value = draft_value` ("Telecommunications service provider")
  → `answers[category]=value` → `_advance(cursor=1, …, message_sid=SM5ed115)`. This is the SOLE
  advance, and it set `last_message_sid=SM5ed115`. ✅ correct behaviour.
- **No inbound exists after 12:44:26** (verified: `max(started_at)=12:44:26`, db_now=13:32).
  So "yes to the city/Mumbai confirm did not advance city" describes an event that **never
  happened** — the owner never replied to the city question.

### 1c. Why the symptom report read it as "Mumbai-confirm yes ignored"

Two confirm questions are adjacent and both `confirm`-kind: category (cursor 0) and city (cursor 1).
From the owner's chat view, the last bot message before "yes" may well have been the **Mumbai**
question (because the five "hi"s kept re-presenting questions, and the conversational greet-back
prepends "Hi! …" to whatever the cursor head was). But the cursor head when the "yes" arrived was
**category** (cursor 0), not city — the live row proves `category` is what got recorded, with the
exact GBP draft string, on the SID of the "yes". The owner's "yes" did advance and record — just
the category, which is the correct, in-order behaviour. The "stuck=1" appearance is a normal 0→1
advance observed mid-journey.

### 1d. Idempotency / redelivery — structurally cannot have caused this

Three independent dedup layers make a redelivery a non-event here, and none of them are the bug:

1. **DBOS workflow level** (twilio_ingress.py:238-244): `workflow_id = "twilio_inbound_{sid}"` +
   `SetWorkflowID(workflow_id)`. `DBOS.start_workflow` no-ops on a known workflow_id → a redelivered
   SID never re-runs `webhook_pipeline_run`. `run_id = uuid5(NAMESPACE_URL, sid)` is deterministic
   on the SID, so a redelivery is idempotent at the RUN level too (same run_id).
2. **`twilio_inbound_events` ledger** (runner.py:407-420 + 619-620): `INSERT … ON CONFLICT
   (message_sid) DO NOTHING` → `newly_inserted=False` → `dupe_status=True`. The journey gate
   (runner.py:715) only fires for `not event.dupe_status`, so a dupe SID never reaches
   `maybe_handle_journey_reply` at all.
3. **`journey.py:155` `last_message_sid` guard** — the third, innermost layer. For it to fire, the
   SAME SID would have to (a) be a new ledger row (not a dupe) AND (b) already equal
   `last_message_sid`. With run_id deterministic on SID, layers 1+2 make that combination
   unreachable in production: a same-SID redelivery is stopped before this branch.

The live ledger confirms **no duplicate SID was ever seen** for this tenant. The idempotency branch
**did not fire**. The batch-plan suspicion ("a Twilio re-delivery / sid-pairing across the
12:42+12:44 runs set last_message_sid to the 'yes' sid on the first of a retry pair") is
**factually false here**: 12:42 (SM0a7a, len 2) and 12:44 (SM5ed115, len 3) are two **different**
messages with different SIDs and different lengths — not a retry pair of one "yes".

### 1e. The one real anomaly found (NOT the reported stall, but worth a row)

The **first** inbound at 10:24:59 (SM20d29, len 2) is a **DBOS workflow stuck in `running`** —
`pipeline_runs.status='running'`, `ended_at=NULL`, only the `webhook_received` step (seq 0)
completed; the run never closed. The five later inbounds completed fine, so this stuck run did
NOT block the journey. It is a separate durability/observability concern (an orphaned workflow,
likely a deploy-restart mid-run around the 12:09 deploy window), not the confirm stall. Flag it,
don't fold it into VT-477.

---

## 2. PROPOSED FIX

**Primary recommendation: there is no functional confirm-advance bug to fix in `journey.py`.**
The per-reply apply/advance/idempotency logic is correct and the live data shows it working.
"Fixing" the idempotency branch would be fixing a non-bug and risks reintroducing the duplicate
"based in Mumbai?" send that 152d24d just removed.

What VT-477 should actually do, in priority order:

### Fix A (the real launch issue behind the confusion) — recompose the STALE queue [= batch plan Defect 1]
The journey is asking a wrong, pre-VT-475 question ("Telecommunications service provider"). The
queue for 63211ce5 was composed 2026-06-27 19:57, before VT-475 (business-type reconciliation)
deployed. VT-475 fixed forward composition but does not recompose existing active queues. This is
the genuine owner-visible defect (a wrong confirm question), and it is what made the journey *look*
broken. **Action:** on each inbound, if the reconciled business-type/label differs from the queued
confirm `draft_value`, lazily recompose the remaining confirm questions (re-derive via
`_compose_queue` against current draft), preserving already-answered fields. Bounded, additive,
no schema change. (Tenant 63211ce5 also needs a one-shot reset — Fazal's call — so it stops asking
the telecom question; see §5.)

### Fix B (hardening, not a bug fix) — make a redelivered confirm provably advance-once
Even though the data shows the idempotency branch never fires in prod, the batch plan asked for an
adversarial test of the redelivered-confirm seam. The current idempotency branch (journey.py:155)
is **already correct** for a true redelivery: same SID == `last_message_sid` → re-emit the CURRENT
(post-advance) question + `already_presented=True` (no double-advance, no re-send). The only seam
worth tightening: the branch keys solely on `last_message_sid`, which is set **only by `_advance`**
— so a redelivery of a message that went down the **greet/re-present** path (which does NOT set
`last_message_sid`) would be reprocessed as a fresh greet (re-present again). That is harmless
(idempotent re-present, no state change, no double-advance) but does cause a duplicate greet-send
on a true greeting redelivery. If we want belt-and-braces, the durable dedup for that case is layers
1+2 (DBOS + `twilio_inbound_events`), which already cover it — so **no journey.py change is
required.** Recommendation: **do NOT touch the idempotency branch.** Document the invariant + add the
adversarial test in §3 as a regression guard.

### How this preserves the 152b24d duplicate-suppression fix
By **not modifying** the idempotency branch or the `already_presented` / `re_present` flags, the
duplicate-question suppression is untouched. Fix A only recomposes queue CONTENT (the confirm
`draft_value`/prompt), never the cursor/apply/send contract. If Fix A is implemented as "recompose
remaining questions whose draft_value is stale," it must preserve `cursor`, `answers`, and
`last_message_sid` (never reset them), so the idempotency marker and the no-double-advance guarantee
survive a recompose.

---

## 3. ADVERSARIAL TEST (regression guard — the seam the existing tests miss)

The existing tests seed clean SIDs and miss the retry-pair ordering. Add a deterministic test that
drives the **confirm → yes → (redelivered same-SID yes)** sequence and asserts advance-exactly-once:

```
seed journey: cursor=0, queue=[confirm city draft=Mumbai, gap hours], answers={}, last_message_sid=None
1. handle_reply(body="yes", sid="SMaaa")   →  asserts:
     - answers["city"] == "Mumbai"          (draft_value recorded, NOT "yes")
     - cursor advanced 0 → 1
     - last_message_sid == "SMaaa"
     - result["already_presented"] is falsy (a FIRST presentation → DOES send the next question)
2. handle_reply(body="yes", sid="SMaaa")   →  REDELIVERY (same sid == last_message_sid). asserts:
     - answers["city"] STILL == "Mumbai"    (no second write)
     - cursor STILL == 1                     (NO double-advance)
     - result["already_presented"] is True   (intercept must NOT re-send)
3. handle_reply(body="yes", sid="SMbbb")   →  the genuine NEXT reply (new sid) at cursor 1 (gap hours):
     - the gap answer is recorded, cursor advances 1 → 2
     - proves a real new reply after a redelivery still advances (not frozen)
```

Plus a flow-level test (mirrors the live data) asserting that **N bare-greeting inbounds with
distinct SIDs do NOT advance the cursor and do NOT set last_message_sid**, and that the FIRST
substantive "yes" with a distinct SID advances exactly once and records the draft_value — i.e. the
exact 5×"hi" + 1×"yes" pattern observed for 63211ce5. This is the test that would have caught the
*misdiagnosis* (it pins the real, correct behaviour).

Key assertions, in one line: **a confirm "yes" records `draft_value` (never "yes") + advances
exactly once; a redelivered SID re-presents without advancing or re-sending; a greeting neither
records, advances, nor sets `last_message_sid`.**

---

## 4. Does VT-479 (rich-media buttons) structurally obviate this?

**Partly — it removes the free-text-parse failure mode for confirms, but it does NOT obviate the
real issue here (the stale queue, Fix A).**

- **What buttons fix:** with a Twilio `twilio/quick-reply` Yes/No/Skip button on a confirm
  question, the owner's answer arrives as a **button payload** (a deterministic token Twilio puts in
  `ButtonText`/`ButtonPayload`), not a free-text "yes" that the `_YES` token-set must match. That
  removes the entire class of "the owner typed something the parser didn't recognise" — including
  Hinglish/typo affirmations the `_YES` set misses, and the greeting-vs-answer ambiguity. So
  structurally it hardens the confirm-answer path.
- **What buttons do NOT fix:**
  1. The **stale queue** (Fix A): a Yes/No button on the WRONG question ("Telecommunications
     service provider?") still asks the wrong thing. Buttons change the input channel, not the
     question content. The recompose is still required.
  2. The **payload still has to map to `draft_value` + advance** — the same `handle_reply`
     apply/advance/idempotency path runs; a button payload of "yes" flows through the same branch.
     The redelivery/idempotency invariant (§3) still must hold for buttons (Twilio can redeliver a
     button-reply webhook too), so the adversarial test stays relevant.
  3. The button-reply webhook is still a normal inbound MessageSid → same dedup layers, same gate.
- **Net:** VT-479 is a strong UX + robustness improvement and should land WITH Fix A, but it is not
  a substitute for Fix A and does not retire the advance/idempotency contract or its test.

---

## 5. Sequencing / actions

1. **Reframe VT-477:** it is NOT a confirm-advance bug. The launch-critical, owner-visible defect is
   the **stale queue** (Fix A / batch-plan Defect 1) — recompose active journey queues whose confirm
   `draft_value` predates VT-475. Plan-first; small; additive.
2. **One-shot reset of 63211ce5's journey** (Fazal's call) so it stops asking the telecom question.
   This is a data fix, not code. Do under the dev-only / EXPECTED_ENV=dev rails; it touches a
   single tenant's `onboarding_journey` row. NO code in this step.
3. **Do NOT modify `journey.py`'s idempotency/advance branch.** Add the §3 regression tests only
   (they pin the already-correct behaviour and would have caught the misdiagnosis).
4. **VT-479 rich-media buttons** — land with Fix A; hardens the confirm input but does not replace it.
5. **Separate row:** the orphaned `running` workflow (SM20d29 @ 10:24:59) — a stuck DBOS run, not
   the stall; investigate durability/restart handling independently.

---

## Reconciliation note (Rule #14)

This investigation **overturns the mechanism** in `onboarding-journey-defects-and-richmedia-plan.md`
§Defect 2. That plan correctly flagged it couldn't see the bodies and asked for a trace; the
`episodic_events.body_length` + the SID/dupe_status ledger supply the missing evidence and show the
redelivery/idempotency hypothesis is false and the journey advanced correctly. The real owner-facing
defect is the stale (pre-VT-475) confirm question, not a broken "yes".
