# WhatsApp Templates — Batch 2 for Meta/Twilio submission (5 templates × EN+HI = 10 SIDs)

**Generated:** 2026-06-07 · Reconciled against `twilio_templates.yaml` (15 live) + the VT-349 in-window doctrine.
All **Utility** category. Copy is a draft for your final word (Pillar-7 honesty gate). Each template below is **self-contained**: body, variables, and Meta's sample values together — no scrolling.
Keep `Viabe`/`Viabe Team`, links and emails in Latin script in both languages.

---

## 1. `support_resolved` — to the OWNER · Utility
**Use:** SupportBot closes the loop after an escalated issue is fixed (arrives hours/days later → out-of-window).
**Variables:** `{{1}}` = support reference id

**EN body:**
```
Good news — your support request {{1}} has been resolved. If anything still
doesn't look right, just reply here and we'll take another look.
```
> **EN sample:** `{{1}}` = `SUP-48213`
> *Renders:* Good news — your support request SUP-48213 has been resolved. If anything still doesn't look right, just reply here and we'll take another look.

**HI body:**
```
अच्छी खबर — आपका सपोर्ट अनुरोध {{1}} हल कर दिया गया है। अगर अब भी कुछ ठीक न लगे,
तो बस यहाँ रिप्लाई करें और हम दोबारा देखेंगे।
```
> **HI sample:** `{{1}}` = `SUP-48213`
> *Renders:* अच्छी खबर — आपका सपोर्ट अनुरोध SUP-48213 हल कर दिया गया है। …

---

## 2. `trial_subscribe_link` — to the OWNER · Utility
**Use:** the day-14+ trial-end nudge carrying the actual payment link (the dormant VT-332 send). Copy matches the real token semantics: 7-day validity, single-use.
**Variables:** `{{1}}` = owner name · `{{2}}` = the subscribe link

**EN body:**
```
Hi {{1}}, here is your payment link to continue with Viabe Team: {{2}}
The link is valid for 7 days and can be used once. If it expires, just reply
here and I'll send a fresh one.
```
> **EN samples:** `{{1}}` = `Rajesh Kumar` · `{{2}}` = `https://viabe.ai/team/subscribe?plan=founding&token=k7Jb2`
> *Renders:* Hi Rajesh Kumar, here is your payment link to continue with Viabe Team: https://viabe.ai/team/subscribe?plan=founding&token=k7Jb2 The link is valid for 7 days…

**HI body:**
```
नमस्ते {{1}}, Viabe Team जारी रखने के लिए आपका पेमेंट लिंक: {{2}}
यह लिंक 7 दिनों तक वैध है और एक ही बार इस्तेमाल हो सकता है। अगर यह एक्सपायर हो
जाए, तो यहाँ रिप्लाई करें और मैं नया भेज दूँगा।
```
> **HI samples:** `{{1}}` = `राजेश कुमार` · `{{2}}` = `https://viabe.ai/team/subscribe?plan=founding&token=k7Jb2`
> *Renders:* नमस्ते राजेश कुमार, Viabe Team जारी रखने के लिए आपका पेमेंट लिंक: https://… 7 दिनों तक वैध…

---

## 3. `dsr_deletion_completed` — to the CUSTOMER · Utility
**Use:** confirms a customer's data-deletion request finished (the purge completes days after the request → out-of-window). Closes the DSR loop with written evidence.
**Variables:** `{{1}}` = business name

**EN body:**
```
This confirms that your personal data held in connection with {{1}} has been
deleted as you requested. No further messages will be sent to you. For any
questions, contact us at info@rkecom.in.
```
> **EN sample:** `{{1}}` = `Alpha Audio`
> *Renders:* This confirms that your personal data held in connection with Alpha Audio has been deleted as you requested. No further messages will be sent to you…

**HI body:**
```
यह पुष्टि है कि {{1}} से जुड़ा आपका व्यक्तिगत डेटा आपके अनुरोध के अनुसार हटा दिया
गया है। आपको अब कोई और संदेश नहीं भेजा जाएगा। किसी भी प्रश्न के लिए हमें
info@rkecom.in पर लिखें।
```
> **HI sample:** `{{1}}` = `अल्फ़ा ऑडियो`
> *Renders:* यह पुष्टि है कि अल्फ़ा ऑडियो से जुड़ा आपका व्यक्तिगत डेटा… हटा दिया गया है।…

---

## 4. `breach_notification_owner` — to the OWNER · Utility
**Use:** mandatory data-breach notice to the business owner. Authored NOW because it cannot be written and Meta-approved during an incident. The variables carry the DPDP-required content (what happened / what data / what we did) so the skeleton fits any incident.
**Variables:** `{{1}}` = owner name · `{{2}}` = what was affected (incident + data categories) · `{{3}}` = action taken / advised step

**EN body:**
```
Hi {{1}}, an important security notice from Viabe Team. We identified a
data-security incident affecting {{2}}. We have {{3}}. We are notifying you as
required under applicable data-protection law and will share updates as we
know more. For questions, write to info@rkecom.in.
```
> **EN samples:** `{{1}}` = `Rajesh Kumar` · `{{2}}` = `your business's customer contact list (names and phone numbers)` · `{{3}}` = `secured the affected system and reset all access credentials`
> *Renders:* Hi Rajesh Kumar, an important security notice from Viabe Team. We identified a data-security incident affecting your business's customer contact list (names and phone numbers). We have secured the affected system and reset all access credentials. We are notifying you…

**HI body:**
```
नमस्ते {{1}}, Viabe Team की ओर से एक महत्वपूर्ण सुरक्षा सूचना। हमें एक डेटा-सुरक्षा
घटना का पता चला है जिसका असर {{2}} पर पड़ा है। हमने {{3}}। लागू डेटा-संरक्षण
क़ानून के अनुसार हम आपको सूचित कर रहे हैं और जैसे-जैसे जानकारी मिलेगी, अपडेट
देंगे। प्रश्नों के लिए info@rkecom.in पर लिखें।
```
> **HI samples:** `{{1}}` = `राजेश कुमार` · `{{2}}` = `आपके व्यवसाय की ग्राहक संपर्क सूची (नाम और फ़ोन नंबर)` · `{{3}}` = `प्रभावित सिस्टम को सुरक्षित कर सभी एक्सेस क्रेडेंशियल रीसेट कर दिए हैं`
> *Renders:* नमस्ते राजेश कुमार, … घटना का पता चला है जिसका असर आपके व्यवसाय की ग्राहक संपर्क सूची (नाम और फ़ोन नंबर) पर पड़ा है। हमने प्रभावित सिस्टम को सुरक्षित कर सभी एक्सेस क्रेडेंशियल रीसेट कर दिए हैं।…

---

## 5. `breach_notification_customer` — to the CUSTOMER · Utility
**Use:** the customer-side breach notice (same rationale: must pre-exist any incident).
**Variables:** `{{1}}` = business name · `{{2}}` = data categories affected · `{{3}}` = advised protective step

**EN body:**
```
Important security notice: a data-security incident at {{1}} may have affected
your {{2}}. We recommend you {{3}}. We are sorry for the concern this may
cause. For questions, contact info@rkecom.in.
```
> **EN samples:** `{{1}}` = `Alpha Audio` · `{{2}}` = `name and phone number` · `{{3}}` = `be cautious of unexpected calls or messages asking for personal information`
> *Renders:* Important security notice: a data-security incident at Alpha Audio may have affected your name and phone number. We recommend you be cautious of unexpected calls or messages asking for personal information.…

**HI body:**
```
महत्वपूर्ण सुरक्षा सूचना: {{1}} में हुई एक डेटा-सुरक्षा घटना का असर आपके {{2}} पर
पड़ा हो सकता है। हमारा सुझाव है कि आप {{3}}। इससे हुई चिंता के लिए हमें खेद है।
प्रश्नों के लिए info@rkecom.in पर संपर्क करें।
```
> **HI samples:** `{{1}}` = `अल्फ़ा ऑडियो` · `{{2}}` = `नाम और फ़ोन नंबर` · `{{3}}` = `व्यक्तिगत जानकारी माँगने वाले अनजान कॉल या संदेशों से सावधान रहें`
> *Renders:* महत्वपूर्ण सुरक्षा सूचना: अल्फ़ा ऑडियो में हुई एक डेटा-सुरक्षा घटना का असर आपके नाम और फ़ोन नंबर पर पड़ा हो सकता है।…

---

## After approval — same wiring as last time
Submit each (EN, then HI) via Twilio Content API → collect the 10 `HX…` SIDs → hand them to Cowork → data-only paste into `twilio_templates.yaml` + `.viabe/templates.md`, byte-verified before merge. `trial_subscribe_link` additionally un-stubs the VT-332 trial-sweep send (CC wires it on SID arrival; the send stays gated on go-live).
