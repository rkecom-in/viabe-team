# Onboarding-journey live defects + rich-media plan (Fazal live-WhatsApp, 2026-06-28)

Source: Fazal drove his real number (+919321553267 = RKeCom onboarding tenant) on dev during the VT-464 re-drive and hit three issues. Investigated against deployed origin/dev HEAD (152b24db) + the live `onboarding_journey` row for tenant 63211ce5.

## Defect 1 — wrong business-type identification ("Telecommunications service provider?")
- **Observed:** journey asked "We found you're a Telecommunications service provider — is that right?"
- **Root cause:** the live `onboarding_journey.question_queue` for tenant 63211ce5 was COMPOSED on 2026-06-27 (`started_at` 19:57), BEFORE VT-475 (bc287bf) deployed. The stale queue still carries the raw GBP category `draft_value='Telecommunications service provider'`.
- **VT-475 status:** the FIX is correct + deployed — `reconcile_business_type(name=RKeCom, gbp='Telecommunications', website=rkecom.in)` → `services`; `question_brain` confirms the reconciled label ("a Local services business"), suppresses the raw category. A FRESH onboarding is correct.
- **The real gap:** VT-475 fixed FORWARD composition but did NOT migrate/recompose EXISTING active journey queues. Any tenant already mid-onboarding keeps the bad question. **Action:** a one-shot recompose (or invalidate-and-rebuild) of active `onboarding_journey.question_queue` rows whose draft predates the VT-475 deploy; OR recompose the confirm questions lazily on next inbound if the reconciled type differs from the queued `draft_value`. Tenant 63211ce5 needs this now (it'll keep asking the telecom question otherwise).

## Defect 2 — journey broke on "yes" to the Mumbai confirm (the headline functional bug)
- **Observed:** journey asked "And you're based in Mumbai — correct?", Fazal replied "yes", and it was NOT understood — the journey did not advance / record `city`.
- **Live state proof:** `cursor=1` (still on the Mumbai/city confirm), `answers={'category':'Telecommunications service provider'}` (NO `city`), `last_message_sid=SM5ed11586` (Fazal's "yes" at 12:44), updated 12:44:31. Two inbound runs from Fazal (12:42 `SM0a7a726e`, 12:44 `SM5ed11586`) landed AFTER the 152b24db deploy (~12:09) yet the journey still stalled.
- **The confirm/advance branch (journey.py:184-195) LOOKS correct** in isolation: `value = draft_value if (toks & _YES)` → 'Mumbai' (draft_value IS present), then `_advance(cursor+1, ...)`. "yes" ∈ `_YES`. So the per-reply logic should record city + advance 1→2. It did not.
- **Therefore the stall is in the message-FLOW, not the branch logic** — the prime suspect is the idempotency early-return (journey.py:155: `message_sid == last_message_sid` → return WITHOUT advancing). A Twilio re-delivery / sid-pairing across the 12:42+12:44 runs plausibly set `last_message_sid` to the "yes" sid on the first of a retry pair, so the second consumed-but-didn't-advance. The redacted dev DB hides the inbound bodies, so the exact step needs either un-redacted local repro or a logfire trace of run e927c669/c34a2d08.
- **152b24db did NOT fix this** — its scope was the "Hi→category" greeting bug + duplicate "Mumbai?" send; the confirm-"yes"-stall is a SEPARATE live defect.
- **Action (new VT, plan-first — this is the launch-critical one):**
  1. Reproduce with a deterministic confirm→yes sequence INCLUDING a same-sid re-delivery (the idempotency path) — the existing tests seed clean sids and miss the retry-pair ordering, same gap class as the VT-464 spawn bug.
  2. Fix the idempotency guard so a re-delivered sid re-PRESENTS the CURRENT question (post-advance) rather than freezing the cursor; ensure a genuine new "yes" always advances + records.
  3. Add a test that drives confirm→yes→(redelivered yes) and asserts `city` recorded + cursor advanced exactly once.

## Defect 3 (Fazal feature directive) — rich-media WhatsApp once the 24h window is open
- **Current:** the journey sends via `send_freeform_message` = plain `Body=` text only. The template path supports `content_sid` (Twilio Content API) but the journey doesn't use it.
- **Fazal's directive:** once the 24h customer-care session window is OPEN (an owner inbound just arrived → window open), our REPLIES should be rich: quick-reply BUTTONS (confirm questions → Yes/No buttons instead of typing "yes"), images, links/CTAs, list pickers.
- **Why this also fixes Defect 2's UX:** a "Yes/No" quick-reply button removes the free-text parse entirely — the confirm answer becomes a button payload, not a token-matched "yes". Strongly recommended to land WITH the Defect-2 fix.
- **Plan (new VT, plan-first):**
  - In-window (owner inbound < 24h ago → free-form session send allowed): build interactive WhatsApp messages via Twilio Content API `twilio/quick-reply` + `twilio/list-picker` + media (`twilio/media`) content types, sent free-form within the open session (no template-approval needed for session messages, but the Content object IS pre-created).
  - Journey confirm questions → quick-reply (Yes / No / Skip) buttons; gap questions with enumerable options → list-picker; the "about" blurb → text + optional image.
  - Out-of-window (>24h, cold) → must use an APPROVED template (content_sid) — keep the existing template path; rich interactivity there needs template approval.
  - Add a window-state helper (last owner-inbound timestamp → in/out of 24h) to pick free-form-rich vs approved-template; the orchestrator already tracks `last_owner_message_at`.
  - Respect the existing rails: session vs marketing send classes (VT-460), `is_customer_session` gating; owner-onboarding sends stay owner-class.

## Sequencing
1. **Defect 2 fix** (launch-critical — onboarding is broken for real owners) — plan-first, new VT.
2. **Defect 1 recompose** of stale active queues — small, can ride with Defect 2 or separately.
3. **Defect 3 rich-media** — new VT, plan-first; ideally lands with Defect 2 (buttons obsolete the "yes" parse).
4. Hygiene: reset tenant 63211ce5's journey so it stops asking the telecom question (Fazal's call).

## Note carried from VT-464 re-drive
A 4th, related live defect was already found: `handoffs.py:42` `_extract_user_request_from_state` reads `messages[0]` expecting a HumanMessage but dispatch prepends SystemMessage blocks → `spawn_sales_recovery` (win-back) crashes. Same test-blind-spot class (tests seed the happy message order). See the VT-464 re-drive #3 report.
