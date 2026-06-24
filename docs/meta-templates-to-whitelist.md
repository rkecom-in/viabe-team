# WhatsApp Templates to Whitelist with Meta (English + Hindi)

**Generated:** 2026-06-06 · Reconciled against `apps/team-orchestrator/config/twilio_templates.yaml` + `.viabe/templates.md`.

> **REVISION (Fazal, 2026-06-06):** Only messages we send **outside** WhatsApp's 24-hour customer-service window need a template. Three of the original eight are direct **replies to a message the owner just sent** (in-window) → they should be **free-form session messages, NOT templates**, and don't need Meta approval at all: **`refund_processing`**, **`support_handoff`**, **`team_edge_case_ack`**. They're removed from the whitelist below (kept at the end as a "do not submit — rewire to free-form" note). **Net to whitelist: 5 templates (10 SIDs).** Rewiring those three to the free-form send path is flagged to CC.

**5 templates** (business-initiated / outside-window) need null SIDs provisioned. Each needs **both English and Hindi** → **10 SIDs** to provision. All are **Utility** category (transactional → faster approval, no marketing opt-in). After Meta approval → upload to Twilio Content API → paste each SID into `twilio_templates.yaml` (`languages: en:` / `hi:`), mirror in `.viabe/templates.md`. Data-only; no code change.

> **Copy is a DRAFT to save you time — yours to finalize** (Pillar-7 honesty gate; edit every word freely). Variables (`{{1}}`, `{{2}}`) are positional and identical across both languages. Keep `Viabe`/`Viabe Team` and the reply keywords (`REFUND`/`CONTINUE`/`DISCUSS`) in Latin script in both versions — the reply classifier reads those tokens reliably (fuller Hindi-keyword reply coverage lands with VT-329).

---

## Summary — WHITELIST THESE 5 (business-initiated, outside-window)

| # | Template | Recipient | Category | Variables | Why a template |
|---|---|---|---|---|---|
| 1 | `trial_ending` | Owner | Utility | `{{1}}` owner name · `{{2}}` trial end date | Proactive day-12 notice |
| 2 | `trial_extension_offered` | Owner | Utility* | `{{1}}` owner name · `{{2}}` new extended end date | Proactive extension notice |
| 3 | `trial_max_reached` | Owner | Utility | `{{1}}` owner name | Proactive expiry notice |
| 4 | `refund_offer` | Owner | Utility | `{{1}}` refund ₹ amount · `{{2}}` response options | Day-39 evaluator pushes it |
| 5 | `refund_completed` | Owner | Utility | `{{1}}` refund ₹ amount | Sent days later (refund clears) — outside window |

\* `trial_extension_offered`: keep wording factual ("I've extended your trial") so Meta keeps it Utility, not Marketing.

## DO NOT submit — rewire to free-form (in-window replies)

| Template | Why no template | Action |
|---|---|---|
| `refund_processing` | Immediate ack to the owner's "REFUND" reply → in-window | Send free-form (`send_whatsapp_message`) |
| `support_handoff` | Reply to the owner's unresolved message → in-window | Send free-form |
| `team_edge_case_ack` | Reply to the owner's exclusion/status message → in-window | Send free-form (also kills the all-variable-body problem) |

The copy for these three (in the sections below) still applies — it's the same message, just sent as a free-form session reply instead of a template. CC will move the send path; no Meta approval needed for them.

---

## Sample values for Meta's example fields

When you submit each template, Meta asks for an example value per `{{n}}`. Use these (amounts and IDs are language-neutral; names and dates are localized). The `₹` symbol is in the template's static text, so the amount sample is **just the number** (no `₹`).

| Template | Var | Sample (English) | Sample (Hindi) |
|---|---|---|---|
| `trial_ending` | `{{1}}` owner name | `Rajesh Kumar` | `राजेश कुमार` |
| | `{{2}}` trial end date | `20 June 2026` | `20 जून 2026` |
| `trial_extension_offered` | `{{1}}` owner name | `Priya Sharma` | `प्रिया शर्मा` |
| | `{{2}}` new end date | `04 July 2026` | `04 जुलाई 2026` |
| `trial_max_reached` | `{{1}}` owner name | `Amit Patel` | `अमित पटेल` |
| `refund_offer` | `{{1}}` refund amount | `2,499` | `2,499` |
| | `{{2}}` response options | `Reply REFUND for the refund, CONTINUE to keep going, or DISCUSS to talk it through` | `रिफंड के लिए REFUND, जारी रखने के लिए CONTINUE, या बात करने के लिए DISCUSS भेजें` |
| `refund_processing` | `{{1}}` refund amount | `2,499` | `2,499` |
| `refund_completed` | `{{1}}` refund amount | `2,499` | `2,499` |
| `support_handoff` | `{{1}}` reference id | `SUP-48213` | `SUP-48213` |
| `team_edge_case_ack` | `{{1}}` reply text | `I've stopped messaging Rajesh's number as you asked.` | `जैसा आपने कहा, मैंने राजेश के नंबर पर मैसेज भेजना बंद कर दिया है।` |

### How each renders with the samples (so you can sanity-check)
- **`trial_ending` (EN):** *Hi Rajesh Kumar, a quick heads-up: your Viabe Team trial ends on 20 June 2026. …*
- **`refund_offer` (EN):** *… You're eligible for a full refund of ₹2,499. Reply REFUND for the refund, CONTINUE to keep going, or DISCUSS to talk it through — whatever you choose is completely fine.*
- **`support_handoff` (HI):** *… वे आपसे व्यक्तिगत रूप से संपर्क करेंगे। ज़रूरत होने पर आपका रेफरेंस SUP-48213 है।*
- **`refund_completed` (HI):** *आपका ₹2,499 का रिफंड प्रोसेस हो गया है और आपकी सदस्यता रद्द कर दी गई है। …*

---

## 1. `trial_ending`
**Variables:** `{{1}}` owner name · `{{2}}` trial end date

**EN:**
```
Hi {{1}}, a quick heads-up: your Viabe Team trial ends on {{2}}. To keep your
agent running campaigns after that, you'll need to add a payment method. I'll send
the details before then — nothing to do right now.
```
**HI:**
```
नमस्ते {{1}}, एक छोटी-सी जानकारी: आपका Viabe Team ट्रायल {{2}} को समाप्त हो रहा
है। इसके बाद भी आपका एजेंट कैंपेन चलाता रहे, इसके लिए आपको एक पेमेंट मेथड जोड़ना
होगा। मैं उससे पहले आपको पूरी जानकारी भेज दूँगा — अभी कुछ करने की ज़रूरत नहीं है।
```

---

## 2. `trial_extension_offered`
**Variables:** `{{1}}` owner name · `{{2}}` new extended end date

**EN:**
```
Hi {{1}}, because your agent ran campaigns during your trial, I've extended it to
{{2}} at no cost. Nothing changes on your end — you'll keep getting weekly
proposals as usual.
```
**HI:**
```
नमस्ते {{1}}, चूँकि आपके एजेंट ने ट्रायल के दौरान कैंपेन चलाए, मैंने आपका ट्रायल
{{2}} तक बिना किसी शुल्क के बढ़ा दिया है। आपकी ओर से कुछ नहीं बदलेगा — आपको हर
हफ़्ते की तरह प्रस्ताव मिलते रहेंगे।
```

---

## 3. `trial_max_reached`
**Variables:** `{{1}}` owner name

**EN:**
```
Hi {{1}}, your Viabe Team trial has now run its full course. To continue, add a
payment method and your agent will pick up right where it left off. Reply here if
you'd like the payment link.
```
**HI:**
```
नमस्ते {{1}}, आपका Viabe Team ट्रायल अब पूरा हो चुका है। जारी रखने के लिए एक
पेमेंट मेथड जोड़ें और आपका एजेंट वहीं से काम शुरू कर देगा जहाँ उसने छोड़ा था। अगर
आपको पेमेंट लिंक चाहिए तो यहाँ रिप्लाई करें।
```

---

## 4. `refund_offer`
**Variables:** `{{1}}` refund ₹ amount · `{{2}}` response options

**EN:**
```
Based on your first 39 days, your campaigns haven't recovered more than you've
paid in fees. You're eligible for a full refund of ₹{{1}}. {{2}} — whatever you
choose is completely fine.
```
**HI:**
```
आपके पहले 39 दिनों के आधार पर, आपके कैंपेन ने आपकी चुकाई गई फ़ीस से ज़्यादा रिकवर
नहीं किया है। आप ₹{{1}} के पूर्ण रिफंड के पात्र हैं। {{2}} — आप जो भी चुनें, पूरी
तरह ठीक है।
```
**Suggested `{{2}}` value at send time:**
- EN: `Reply REFUND for the refund, CONTINUE to keep going, or DISCUSS to talk it through`
- HI: `रिफंड के लिए REFUND, जारी रखने के लिए CONTINUE, या बात करने के लिए DISCUSS भेजें`

---

## 5. `refund_processing`
**Variables:** `{{1}}` refund ₹ amount

**EN:**
```
Your refund of ₹{{1}} is being processed. It should reach your original payment
method within 5 business days. I'll confirm once it's done.
```
**HI:**
```
आपका ₹{{1}} का रिफंड प्रोसेस किया जा रहा है। यह 5 कार्य-दिवसों के भीतर आपके मूल
पेमेंट मेथड में पहुँच जाना चाहिए। पूरा होने पर मैं पुष्टि कर दूँगा।
```

---

## 6. `refund_completed`
**Variables:** `{{1}}` refund ₹ amount

**EN:**
```
Your refund of ₹{{1}} has been processed and your subscription is cancelled. Your
dashboard stays available for 30 days if you'd like to export anything. Thanks for
trying Viabe Team.
```
**HI:**
```
आपका ₹{{1}} का रिफंड प्रोसेस हो गया है और आपकी सदस्यता रद्द कर दी गई है। अगर आप
कुछ एक्सपोर्ट करना चाहें तो आपका डैशबोर्ड 30 दिनों तक उपलब्ध रहेगा। Viabe Team
आज़माने के लिए धन्यवाद।
```

---

## 7. `support_handoff`
**Variables:** `{{1}}` reference id

**EN:**
```
Thanks for your message. This one needs a human, so I've flagged it to a customer
service representative, who will follow up with you personally. Your reference is
{{1}} if you need to mention it.
```
**HI:**
```
आपके संदेश के लिए धन्यवाद। इसके लिए किसी व्यक्ति की ज़रूरत है, इसलिए मैंने इसे
हमारे ग्राहक सेवा प्रतिनिधि को भेज दिया है, जो आपसे व्यक्तिगत रूप से संपर्क करेंगे।
ज़रूरत होने पर आपका रेफरेंस {{1}} है।
```
> Honest by design — **no time promise** (the SLA-backed "within X hours" wording is Phase-2). Reframed from "Fazal" to "a customer service representative" (Fazal, 2026-06-06) so it scales beyond the founder.

---

## 8. `team_edge_case_ack`  — ⚠️ NEEDS A DECISION BEFORE SUBMISSION
The current design has the body = just `{{1}}` (a generic carrier for the handler's reply text). **Meta rejects all-variable bodies**, so it needs static framing. The version below uses the **static-wrapper option (a)** I recommended — submit this only if you choose (a):

**Variables:** `{{1}}` reply text

**EN:**
```
Update from your Viabe agent: {{1}}
```
**HI:**
```
आपके Viabe एजेंट की ओर से अपडेट: {{1}}
```
> Alternative (b): replace this generic carrier with per-intent templates (exclusion-confirmed, status-answer, …), each with full static copy — cleaner but more templates. Your call; CC will adjust the handler to match.

---

## After Meta approval — wiring (CC/me, data-only)
1. Meta approves (EN, then HI) → 2. upload to Twilio Content API → SID (`HX…`) → 3. paste into `twilio_templates.yaml` `languages:` (replace `null`) → 4. mirror in `.viabe/templates.md`. Fail-closed path flips live automatically.

## Not in this batch (Phase-2)
`refund_discuss_ack` (DISCUSS-branch ack) and `support_resolved` (SupportBot "resolved" notice) are deferred — not built yet, don't submit now.
