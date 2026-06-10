# Viabe Team — WhatsApp Template Registry

**Source of truth** for Meta-approved WhatsApp templates with their Twilio Content SIDs. **Cowork-managed.** This is the authoritative map from `template_name` → `twilio_content_sid` + body text that the runtime will reference when sending WhatsApp messages.

**Owner:** Cowork. Established 2026-05-26 IST as the HUMAN-readable companion to `apps/team-orchestrator/config/twilio_templates.yaml` (which already existed since VT-3.3c shipped). The yaml is the runtime source of truth (name → SID for code); this markdown file adds the bodies, variable signatures, tier categorization, and approval-status notes that the yaml deliberately doesn't carry. When templates are added or rotated, BOTH files update in lockstep.

**VT-163 update (2026-05-31):** Variable signatures are now ALSO machine-readable in `twilio_templates.yaml` under the `variables:` key (ordered list of snake_case param names; index i = positional `{{i+1}}`). This file remains the human-readable companion with message body copy. The yaml `variables:` lists are sourced from the signatures documented here. They update in lockstep — when a template is added or its signature changes, update both.

**Correction to my earlier framing 2026-05-26 17:05 IST:** I initially wrote this file claiming the 8 SIDs had "no repo presence" — that was a grep-mistake on my side; the yaml has had them since VT-3.3c. The actual gap is that the COPY (message text) + variable signatures + tier categorization aren't in either the yaml OR the Meta/Twilio consoles in a way that Cowork or Claude Code can reference at brief-time. THIS file closes THAT gap.

**Source authority:** Meta-approval status owned by Fazal (vendor relationship). Twilio Content SIDs are the canonical runtime identifier — assigned when the template is uploaded to Twilio Content API after Meta approval. The `template_name` is the human-readable handle used in code.

## Why this file exists in this place

WhatsApp templates have a registration lifecycle outside the codebase: author → Meta review → approve → upload to Twilio → get a SID. The SID is the only thing the runtime cares about. Without a durable repo-side record of the `template_name` → `SID` mapping:
- Code that wants to send a template can only hard-code SIDs (fragile)
- Status drift between Meta + Twilio + repo is invisible
- New templates get added without process

This file becomes the contract. Code in `apps/team-orchestrator/` (when wired) references this file (or a YAML mirror generated from it) to resolve `template_name → SID` at call time.

## Status (2026-05-26 IST)

**8 templates Meta-approved + Twilio-registered.** Per CL-5 / CL-11: target counts are 5 launch-blocking Tier-A + 17 Tier-B concierge-until-approved. The 8 below cover the 5 Tier-A (best estimate; Fazal-confirm categorization) plus 3 operational fallbacks.

---

## Approved templates

### `team_welcome`

- **Twilio Content SID:** `HX1b66c0daaa52dc0b8575e50eebadfdd1`
- **Tier:** Tier-A (launch-blocking, onboarding flow)
- **Variables:** `{{1}}` = owner name, `{{2}}` = trial end date

```
Hi {{1}}, your Viabe Team account is now active. Your trial period ends on {{2}}.
During this period, your agent will review your business data and prepare its first
campaign proposal for your review. You'll receive a WhatsApp message here when the
proposal is ready.
```

---

### `team_weekly_approval`

- **Twilio Content SID:** `HX44b053c946a230ea0d2d3d2dc6118964`
- **Tier:** Tier-A (launch-blocking, core proposal flow)
- **Variables:** `{{1}}` = customer segment, `{{2}}` = campaign mode, `{{3}}` = projected recovery ₹

```
This week I'd like to run a {{2}} campaign targeting {{1}} customers. Based on
similar campaigns, this could recover approximately ₹{{3}} in revenue. Reply YES
to approve, NO to skip this week, or EDIT to discuss changes. I'll wait for your
reply before sending anything.
```

---

### `team_opt_out_confirmation`

- **Twilio Content SID:** `HX6365c429e75c2e191bf396e1c6ba8708`
- **Tier:** Tier-A (compliance, customer-paused flow)
- **Variables:** `{{1}}` = owner name

```
Got it, {{1}}. I've paused all automated messages and campaigns immediately. Your
subscription remains active for billing purposes, but I won't initiate anything new
until you tell me to restart. To resume, reply START. To cancel your subscription
entirely, reply CANCEL and I'll process that for you. Thanks for letting me know.
```

---

### `team_dsr_acknowledgment`

- **Twilio Content SID:** `HXcda0b9bb6ea92c072fb8eb7d06163ef0`
- **Tier:** Tier-A (DPDP compliance, mandatory acknowledgment within 30 days)
- **Variables:** `{{1}}` = owner name, `{{2}}` = DSR type (e.g. "data access", "deletion"), `{{3}}` = completion deadline date

```
Hi {{1}}, I've received your {{2}} request. Per the Digital Personal Data Protection
Act, I have 30 days to respond fully. I'll complete your request by {{3}} and
confirm here on WhatsApp once done. If you have questions in the meantime, reply
to this message and I'll get back to you within one business day.
```

---

### `team_agent_stuck_escalation`

- **Twilio Content SID:** `HX6f15db7fee7037c570ba122387f39b10`
- **Tier:** Tier-A or Tier-B (operational fallback — agent surfaces uncertainty; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = what the agent got stuck on, `{{3}}` = context

```
Hi {{1}}, I got stuck on {{2}}. {{3}}. Can you reply here with your guidance? Or
if you'd prefer to talk to a human directly, reply ESCALATE and someone from the
Viabe team will reach out to you. I'd rather pause and ask than guess and get it
wrong.
```

---

### `team_unable_to_complete_request`

- **Twilio Content SID:** `HXb545fe12033d79293f61bc614baa4caf`
- **Tier:** Tier-A or Tier-B (operational fallback; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = request description, `{{3}}` = failure reason

```
Hi {{1}}, we tried to complete your request: {{2}}. It didn't go through because:
{{3}}. We'll keep working on it and update you. If you'd like to change the
approach, just reply here.
```

---

### `team_error_handler`

- **Twilio Content SID:** `HXe9212e16b8647a5d9ab6fcff647bf600`
- **Tier:** Tier-A or Tier-B (system-level fallback; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = action we tried, `{{3}}` = possible reasons

```
Hi {{1}}, we've been trying to {{2}} but keep running into issues. Possible reasons:
{{3}}. We've paused this for now. Reply here with how you'd like us to proceed, or
we'll try a different approach in 24 hours.
```

---

### `team_status_ping`

- **Twilio Content SID:** `HX11199e6fc93eaa1f8b26071995614476`
- **Tier:** Tier-A (keep-alive ping during quiet weeks; Fazal-confirm)
- **Variables:** `{{1}}` = owner name, `{{2}}` = last activity description, `{{3}}` = next-up description

```
Hi {{1}}, things are running. Last activity on your account: {{2}}. {{3}} is up next.
```

---

### `team_campaign_not_sent`  *(VT-248 — SYSTEM-invoked on fail-closed campaign rejection, NOT agent-selectable)*

- **Twilio Content SID:** `HXcedcda2a0bc1e8f47b37950ef458feb4` (en) / `HXcd2688e6ea1862c063378b18e382e700` (hi)
- **Category:** Utility · **Content type:** Text
- **Variables:** `{{1}}` = owner name, `{{2}}` = count of targets that couldn't be verified
- **Privacy invariant (VT-241):** the owner sees the COUNT only — never ids, never a cross-tenant distinction. The full rejected-id list stays in the operator audit log.

```
Hi {{1}}, I couldn't send this week's campaign: {{2}} of the targeted customers couldn't be verified, so I held the entire campaign — nothing was sent. Reply here to retry or adjust the targeting.
```

---

## Implications for code

When code in `apps/team-orchestrator/` starts sending WhatsApp messages (currently only the orchestrator + supervisor + SR-Agent skeleton exist; output composer VT-30 is Backlog), it needs:

1. **A canonical Python mapping** `TEMPLATE_SIDS: dict[str, str]` (probably at `apps/team-orchestrator/src/orchestrator/templates.py`)
2. **Variable validation** — each template's positional variables documented + checked at send-time
3. **No hard-coded SIDs scattered through code** — single import surface

That's a future VT row (likely **VT-178** when filed) — *"WhatsApp template registry as Python module + Twilio send wrapper."* Wires this `.viabe/templates.md` registry into the runtime. Depends on VT-30 (Composer) probably; possibly earlier.

For now, this markdown file IS the registry. CC reads it when needed; Cowork edits it when new templates land.

## Tier-A vs Tier-B categorization (Fazal-confirm needed)

Per CL-5: target counts are 5 Tier-A launch-blocking + 17 Tier-B concierge-until-approved (per CL-11, count grew to 22 total — current 8 + future 14). My best-guess Tier-A categorization above; Fazal-confirm at next pass.

## Status history

- 2026-05-26 17:05 IST: file created by Cowork. 8 approved templates + Twilio Content SIDs recorded from Fazal-provided list. Substrate gap closed (CL-5 + CL-11 referenced template counts but never the SIDs).

## Hindi (hi) variants + team_monthly_report (VT-163-fix-1/2/3)

Twilio issues a SEPARATE SID per language; the registry key is `(template_name, language) -> content_sid` (config/twilio_templates.yaml). Keywords (YES/NO/EDIT/START/CANCEL/ESCALATE) stay English (literal handler triggers).

### `team_monthly_report`  *(VT-163-fix-2 — system-invoked by VT-86, not agent-selectable)*

- **Twilio Content SID:** `HX7a247e236782425866a8e20fd78df275` (en) / `HX252be212f9372e187caa03df117adc02` (hi)
- **Type:** Media (document/PDF header) + body. Category: Utility.
- **Variables:** `{{1}}` = owner name, `{{2}}` = month, `{{3}}` = recovered ₹

```
Hi {{1}}, your Viabe Team report for {{2}} is ready — I've attached the full PDF. It covers the campaigns I ran with your approval, the customers reached, and the revenue attributed to them: ₹{{3}} this month. Tap the document above to view the details.
```

```
नमस्ते {{1}}, {{2}} के लिए आपकी Viabe Team रिपोर्ट तैयार है — मैंने पूरी PDF संलग्न कर दी है। इसमें वे कैंपेन शामिल हैं जो मैंने आपकी मंज़ूरी से चलाए, कितने ग्राहकों तक पहुँचा गया, और उनसे जुड़ा राजस्व: इस महीने ₹{{3}}। विवरण देखने के लिए ऊपर दिए दस्तावेज़ पर टैप करें।
```

---

### Hindi bodies for the 8 existing templates

**`team_welcome` [hi]** — `HXf154fc0f582955f65c75b6306662388a`

```
नमस्ते {{1}}, आपका Viabe Team अकाउंट अब सक्रिय हो गया है। आपकी ट्रायल अवधि {{2}} को समाप्त होगी। इस दौरान, आपका एजेंट आपके व्यवसाय के डेटा की समीक्षा करेगा और आपकी समीक्षा के लिए अपना पहला कैंपेन प्रस्ताव तैयार करेगा। प्रस्ताव तैयार होने पर आपको यहीं WhatsApp पर संदेश मिलेगा।
```

**`team_weekly_approval` [hi]** — `HX4c63feb64d392ada48b0fe11cb1d067d`

```
इस हफ़्ते मैं {{1}} ग्राहकों को लक्षित करते हुए एक {{2}} कैंपेन चलाना चाहता हूँ। इसी तरह के कैंपेन के आधार पर, इससे लगभग ₹{{3}} का राजस्व वापस मिल सकता है। मंज़ूरी देने के लिए YES लिखें, इस हफ़्ते छोड़ने के लिए NO, या बदलाव पर चर्चा के लिए EDIT लिखें। कुछ भी भेजने से पहले मैं आपके जवाब का इंतज़ार करूँगा।
```

**`team_opt_out_confirmation` [hi]** — `HX960b6de9033e0a5954a38fc09b25da2b`

```
ठीक है, {{1}}। मैंने सभी स्वचालित संदेश और कैंपेन तुरंत रोक दिए हैं। बिलिंग के लिए आपकी सदस्यता सक्रिय रहेगी, लेकिन जब तक आप दोबारा शुरू करने के लिए नहीं कहते, मैं कुछ भी नया शुरू नहीं करूँगा। फिर से शुरू करने के लिए START लिखें। अपनी सदस्यता पूरी तरह रद्द करने के लिए CANCEL लिखें और मैं उसे प्रोसेस कर दूँगा। बताने के लिए धन्यवाद।
```

**`team_dsr_acknowledgment` [hi]** — `HXac6e8f1193d97252c1afeb3516d4c9b6`

```
नमस्ते {{1}}, मुझे आपका {{2}} अनुरोध मिल गया है। डिजिटल पर्सनल डेटा प्रोटेक्शन अधिनियम के अनुसार, मेरे पास पूरी तरह जवाब देने के लिए 30 दिन हैं। मैं आपका अनुरोध {{3}} तक पूरा करूँगा और पूरा होने पर यहीं WhatsApp पर पुष्टि करूँगा। इस बीच कोई सवाल हो, तो इस संदेश का जवाब दें और मैं एक कार्यदिवस के भीतर आपसे संपर्क करूँगा।
```

**`team_agent_stuck_escalation` [hi]** — `HX913b93eecd3bf9401116365f268a1008`

```
नमस्ते {{1}}, मैं {{2}} पर अटक गया हूँ। {{3}}। क्या आप यहाँ अपना मार्गदर्शन देकर जवाब दे सकते हैं? या अगर आप सीधे किसी व्यक्ति से बात करना चाहें, तो ESCALATE लिखें और Viabe टीम का कोई सदस्य आपसे संपर्क करेगा। ग़लत अनुमान लगाकर गलती करने से बेहतर है कि मैं रुककर पूछूँ।
```

**`team_unable_to_complete_request` [hi]** — `HXa232c5bc481f90bb5f8b32d05591859a`

```
नमस्ते {{1}}, हमने आपका अनुरोध पूरा करने की कोशिश की: {{2}}। यह इस वजह से पूरा नहीं हो सका: {{3}}। हम इस पर काम करते रहेंगे और आपको अपडेट देंगे। अगर आप तरीका बदलना चाहें, तो यहीं जवाब दें।
```

**`team_error_handler` [hi]** — `HXe02bb244729c5e829fcad2453e0262ec`

```
नमस्ते {{1}}, हम {{2}} करने की कोशिश कर रहे हैं लेकिन बार-बार दिक्कतों का सामना कर रहे हैं। संभावित कारण: {{3}}। हमने फ़िलहाल इसे रोक दिया है। आप कैसे आगे बढ़ना चाहते हैं यह यहाँ जवाब देकर बताएँ, वरना हम 24 घंटे में कोई दूसरा तरीका आज़माएँगे।
```

**`team_status_ping` [hi]** — `HXa386953554630e233f5875299f2d2c94`

```
नमस्ते {{1}}, सब कुछ ठीक चल रहा है। आपके अकाउंट पर आख़िरी गतिविधि: {{2}}। अगला कदम: {{3}}।
```

**`team_campaign_not_sent` [hi]** — `HXcd2688e6ea1862c063378b18e382e700`  *(VT-248)*

```
नमस्ते {{1}}, मैं इस हफ़्ते का कैंपेन नहीं भेज सका: लक्षित ग्राहकों में से {{2}} की पुष्टि नहीं हो सकी, इसलिए मैंने पूरा कैंपेन रोक दिया — कुछ भी नहीं भेजा गया। दोबारा कोशिश करने या लक्ष्यीकरण बदलने के लिए यहाँ उत्तर दें।
```

## Business-initiated owner templates (VT-45-wire, Fazal 2026-06-06)

The 5 owner-facing business-initiated templates (out-of-window) — SIDs provisioned by Fazal
2026-06-06, wired into `twilio_templates.yaml` (was fail-closed `null`). **Cowork: the
Meta-approved BODY COPY (EN + HI) for these 5 lives in the Twilio/Meta console; add the body
text + variable signatures here at the next pass — I have the SID map, not the approved copy.**
The 3 in-window acks (`refund_processing`, `support_handoff`, `team_edge_case_ack`) are NOT
templates — they become free-form sends in VT-349 and are removed from the registry there.

| template | tier | en SID | hi SID |
|---|---|---|---|
| `trial_ending` | VT-90 trial lifecycle | `HX7a7e4a40e500b632b65d4060d62da592` | `HX93ceca39d063ce4eaebefbc6751e01b3` |
<!-- VT-365 (Fazal 2026-06-09): removed `trial_extension_offered`, `trial_max_reached` (no extensions),
     `refund_offer` (VT-85 day-39), `refund_completed` (VT-93) — the refund subsystem + trial extensions
     are gone. 30-day flat trial → `trial_ending` warn → subscribe-or-lapse. SIDs retired in Twilio. -->

| `support_resolved` | VT-108 batch-2 · SupportBot resolve (owner) | `HX4a14a1dc0e84beeee383094c5d47942a` | `HXd3a19118d25953cc77ee8915b32099a6` |
| `trial_subscribe_link` | VT-108 batch-2 · trial-end pay link (owner; VT-332 send) | `HX3c61f10c65156d381438c265b09474a9` | `HX3d8bb10b75c83d0ebc9310d66504e729` |
| `dsr_deletion_completed` | VT-108 batch-2 · DSR purge confirmation (customer) | `HXa2aada217c00112c386966f8daa1984c` | `HX60e633af93225f5e46c78203e0b99c44` |
| `breach_notification_owner` | VT-108 batch-2 · breach notice (owner) — incident-use only, ops path, never agent_selectable | `HX269a7f69da791f24b4cee23bd820383e` | `HX1b4a5c64f7f4c3d07c0ba8798fa120bf` |
| `breach_notification_customer` | VT-108 batch-2 · breach notice (customer) — incident-use only, ops path, never agent_selectable | `HXdbf0129d38d60d57b11851d8acf581e6` | `HX48dcbb5f65877f8592296921b3bad100` |
