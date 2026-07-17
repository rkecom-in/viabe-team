# O10-2 — Welcome template Meta UTILITY package (Fazal submit-pack, prepared by CC 2026-07-18)

## STEP 0 — check current status FIRST (2 min, console)
Twilio Console → Messaging → Content Template Builder → `team_welcome4`
(SIDs: en `HXc8188616b2e97b557f4c7330157c4f8f` · hi `HXd8a8d5945c79c75d373d9c24edd4b183`).
Submitted 2026-07-02 as UTILITY, approval was ASYNC (`status=received`). Read the CURRENT
`approval_requests` status per language:

| Status you see | Action |
|---|---|
| **approved + category UTILITY** | DONE — nothing to submit. Tell CC; O10-2 closes. (History note: welcome3 was approved then FORCE-CONVERTED to MARKETING later — re-check category, not just approval.) |
| **approved but category MARKETING** (force-converted, the welcome3 fate) | Submit the fresh template below (do NOT appeal — the appeal path was dropped 2026-07-02, your call, it stands). |
| **rejected / pending >7d** | Submit the fresh template below. |

## IF resubmission needed — `team_welcome5` (copy tightened further toward pure-transactional)

**Why welcome4's copy might have converted:** "your Viabe account has been created" + a setup
button is already minimal, but Meta's classifier reads brand-name-led sentences + CTA buttons as
promotional signals in edge cases. welcome5 removes the brand from the body (sender identity
already carries it) and keeps ONE next-step.

- **Category:** UTILITY. **Type:** `twilio/quick-reply`. **Variables:** `{{1}}` = owner name ONLY.
- **Copy (en):** `Hi {{1}}, your account setup is incomplete. Tap below to finish the required steps.`
- **Copy (hi):** `नमस्ते {{1}}, आपका अकाउंट सेटअप अधूरा है। ज़रूरी स्टेप पूरे करने के लिए नीचे टैप करें।`
- **Button (en/hi):** `Complete Setup` / `सेटअप पूरा करें` (id/payload `COMPLETE_SETUP`, both langs —
  the journey's continue-trigger key, unchanged).
- **Classification rationale (paste if a justification field exists):** "Post-signup transactional
  notification: informs the user their just-initiated account setup is incomplete and provides the
  single required next step. No promotion, offer, pricing, or marketing content."

## Submit steps (Console path — mirrors the vt555 flow but no CLI needed)
1. Content Template Builder → Create → name `team_welcome5` → type Quick Reply → language en →
   paste copy + button → category UTILITY → submit for WhatsApp approval.
2. Repeat for hi.
3. Send CC both new HX SIDs → CC updates `.viabe/templates.md` + `templates_registry` routing
   (`welcome` → welcome5) + the `_default_welcome` path, EN/HI variant map — one small PR, no other
   code change (VT-555 wiring is name-indirected).

## NOT in this package
- hi-Latn (Hinglish) welcome variant — separate D1 item; add as a THIRD language on whichever
  template survives, once you do the Meta hi-Latn batch.
