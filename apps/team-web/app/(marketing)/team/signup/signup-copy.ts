/**
 * Bilingual copy for the signup wizard, from the Claude Design prototype
 * ("Viabe Reports" project, `Signup Flow.dc.html`).
 *
 * THE PROTOTYPE'S CONTENT IS PLACEHOLDER. Everything below is either the real thing or
 * says plainly that it is not:
 *
 *  1. `c1body` / `c2body` — the prototype writes substantive DPDP and residency
 *     disclosure text and it is not ours to author. Instead the expander carries a
 *     factual SUMMARY of what each consent covers (`c1summary` / `c2summary`, drawn
 *     from what the system actually does), a link to the real disclosure page, and
 *     `disclosureDraft` stating that the binding text is still with counsel — which
 *     is what `/team/dpdp` and `/team/privacy` say about themselves today.
 *  2. `c1version` / `c2version` — the prototype invents "Disclosure v2.3 · 14 Jan
 *     2026". The real identifiers are server-owned in
 *     `apps/team-orchestrator/config/disclosure_versions.yaml` and are what actually
 *     gets written to `consent_records`. They are surfaced from the server, never
 *     typed here, so the line the owner reads can never drift from the row we store.
 *  3. `bt1`–`bt10` — the prototype invents a business-type list. The real taxonomy is
 *     server-owned (`/api/team/business-types`, `label_en` / `label_hi`) and is
 *     constrained so the L3 k-anon cohorts stay populated (VT-82).
 *  4. `td1`–`td4` — the prototype invents four post-signup tasks. The real next steps
 *     are the onboarding wizard's (VT-267 PR-C): check the drafted profile, connect a
 *     sales-data source, connect WhatsApp. Three, not four.
 *  5. The footer's "Viabe Technologies · DPDP-compliant" — the entity is RKeCom
 *     Services (OPC) Pvt Ltd, and a DPDP-compliance claim is not supportable while the
 *     legal pages say they do not yet bind.
 */

export type Lang = 'en' | 'hi'
type Entry = { en: string; hi: string }

export const COPY = {
  langLabel: { en: 'Language', hi: 'भाषा' },
  h1: {
    en: 'Set up your AI sales and marketing team',
    hi: 'अपनी AI सेल्स और मार्केटिंग टीम तैयार करें',
  },
  sub: {
    en: 'Four steps. We check your business against the GST registry before we create your account.',
    hi: 'चार चरण। खाता बनाने से पहले हम आपके व्यापार को GST रजिस्ट्री से जाँचते हैं।',
  },
  railNote: {
    en: 'Your account is created only after your WhatsApp number is verified. Nothing is charged on this page.',
    hi: 'आपका खाता तभी बनेगा जब आपका WhatsApp नंबर सत्यापित हो जाएगा। इस पेज पर कोई शुल्क नहीं लिया जाता।',
  },

  // --- step rail ---
  s1: { en: 'Your details', hi: 'आपकी जानकारी' },
  s2: { en: 'Find your business', hi: 'अपना व्यापार खोजें' },
  s3: { en: 'Verify WhatsApp', hi: 'WhatsApp सत्यापन' },
  s4: { en: 'Ownership review', hi: 'स्वामित्व समीक्षा' },
  s1note: { en: 'Before an account exists', hi: 'खाता बनने से पहले' },
  s2note: { en: 'GST registry check', hi: 'GST रजिस्ट्री जाँच' },
  s3note: { en: 'This creates your account', hi: 'इससे आपका खाता बनता है' },
  s4note: { en: 'Reviewed by a person', hi: 'व्यक्ति द्वारा समीक्षा' },
  stepOf: { en: 'Step {n} of 4', hi: 'चरण {n} / 4' },
  nextUp: { en: 'Next: {title}', hi: 'आगे: {title}' },

  // --- step 1 ---
  s1sub: {
    en: 'We need these to search public records and to send your verification code.',
    hi: 'सार्वजनिक रिकॉर्ड खोजने और सत्यापन कोड भेजने के लिए ये जानकारी ज़रूरी है।',
  },
  ownerName: { en: 'Your name', hi: 'आपका नाम' },
  ownerNamePh: { en: 'As you sign', hi: 'जैसे आप हस्ताक्षर करते हैं' },
  businessName: { en: 'Business name', hi: 'व्यापार का नाम' },
  businessNamePh: { en: 'The name customers know', hi: 'जिस नाम से ग्राहक जानते हैं' },
  businessType: { en: 'Business type', hi: 'व्यापार का प्रकार' },
  selectPh: { en: 'Choose the closest match', hi: 'सबसे नज़दीकी विकल्प चुनें' },
  city: { en: 'City', hi: 'शहर' },
  cityPh: { en: 'Where you operate', hi: 'जहाँ आप काम करते हैं' },
  whatsapp: { en: 'WhatsApp number', hi: 'WhatsApp नंबर' },
  whatsappHint: {
    en: 'Your verification code goes to this number.',
    hi: 'सत्यापन कोड इसी नंबर पर आएगा।',
  },
  email: { en: 'Email (optional)', hi: 'ईमेल (वैकल्पिक)' },
  emailHint: {
    en: 'If you give it, your consent record is sent here.',
    hi: 'यदि आप देते हैं, तो सहमति का रिकॉर्ड यहीं भेजा जाएगा।',
  },
  // 2026-08-21 (Fazal): the header toggle already sets the INTERFACE language, so the form does not
  // ask that again. What it asks instead is the language the AGENTS should message the owner in —
  // a different decision, and the one the product actually needs. Three options, because Hinglish
  // is a real register here (owner_locale's value space is en | hinglish | hi).
  commsLang: { en: 'Which language should we message you in?', hi: 'हम आपसे किस भाषा में बात करें?' },
  commsLangHint: {
    en: 'How your agents write to you — updates, approvals and questions. You can change it any time.',
    hi: 'आपके एजेंट आपसे कैसे बात करेंगे — अपडेट, मंज़ूरी और सवाल। आप इसे कभी भी बदल सकते हैं।',
  },
  langEn: { en: 'English', hi: 'English' },
  langHi: { en: 'हिन्दी (Devanagari)', hi: 'हिन्दी (देवनागरी)' },
  langHinglish: { en: 'Hinglish (Hindi in English letters)', hi: 'Hinglish (रोमन लिपि में हिन्दी)' },
  langHinglishEg: { en: 'e.g. "Aapke 12 customers ne 45 din se order nahi kiya"', hi: 'जैसे "Aapke 12 customers ne 45 din se order nahi kiya"' },

  // --- consents (two separate decisions, neither pre-ticked) ---
  consentH: { en: 'Two consents', hi: 'दो सहमतियाँ' },
  consentNote: {
    en: 'These are separate decisions. Both are required, and neither is ticked for you.',
    hi: 'ये दो अलग निर्णय हैं। दोनों आवश्यक हैं, और कोई भी पहले से चुना नहीं गया है।',
  },
  c1title: { en: 'Data processing under the DPDP Act', hi: 'DPDP अधिनियम के तहत डेटा प्रोसेसिंग' },
  c1accept: {
    en: 'I allow Viabe to process my business and customer data so my agents can work.',
    hi: 'मैं Viabe को अपने व्यापार और ग्राहकों का डेटा प्रोसेस करने की अनुमति देता/देती हूँ, जिससे मेरे एजेंट काम कर सकें।',
  },
  c2title: { en: 'India data residency', hi: 'भारत में डेटा निवास' },
  c2accept: {
    en: 'I accept that my data is stored in India, and that named vendors may process parts of it outside India.',
    hi: 'मैं स्वीकार करता/करती हूँ कि मेरा डेटा भारत में रखा जाएगा, और नामित वेंडर उसका कुछ हिस्सा भारत के बाहर प्रोसेस कर सकते हैं।',
  },
  readMore: { en: 'Read the disclosure', hi: 'प्रकटीकरण पढ़ें' },
  hideMore: { en: 'Hide the disclosure', hi: 'प्रकटीकरण छिपाएँ' },
  disclosureLink: { en: 'Open the full disclosure', hi: 'पूरा प्रकटीकरण खोलें' },
  disclosureDraft: {
    en: 'The full text is still under review by our legal counsel, so it does not yet bind you or Viabe. The version identifier above is what we record against your consent, and it will not change when the text is finalised.',
    hi: 'पूरा पाठ अभी हमारे कानूनी सलाहकार की समीक्षा में है, इसलिए यह अभी आप पर या Viabe पर बाध्यकारी नहीं है। ऊपर दिया गया संस्करण पहचानकर्ता वही है जो आपकी सहमति के साथ दर्ज होता है, और पाठ अंतिम होने पर वह नहीं बदलेगा।',
  },
  c1summary: {
    en: 'Covers the customer names, phone numbers and message history your agents read and write. You remain the data fiduciary for your customers; Viabe processes on your instruction. You can withdraw this from your workspace, which stops the agents.',
    hi: 'इसमें वे ग्राहक नाम, फ़ोन नंबर और संदेश इतिहास शामिल हैं जिन्हें आपके एजेंट पढ़ते और लिखते हैं। अपने ग्राहकों के लिए डेटा फ़िड्यूशरी आप ही रहते हैं; Viabe आपके निर्देश पर प्रोसेस करता है। आप इसे अपने वर्कस्पेस से वापस ले सकते हैं, जिससे एजेंट रुक जाएँगे।',
  },
  c2summary: {
    en: 'Your account, messages and business records are stored on servers in India. Some processing happens outside India — the language model that drafts replies is the one that affects every message. The disclosure names each processor.',
    hi: 'आपका खाता, संदेश और व्यापार रिकॉर्ड भारत के सर्वरों पर रखे जाते हैं। कुछ प्रोसेसिंग भारत के बाहर होती है — उत्तर तैयार करने वाला भाषा मॉडल हर संदेश को प्रभावित करता है। प्रकटीकरण में हर प्रोसेसर का नाम है।',
  },
  recordedAs: { en: 'Recorded as', hi: 'इस रूप में दर्ज' },

  step1Cta: { en: 'Continue to find my business', hi: 'आगे बढ़ें — मेरा व्यापार खोजें' },
  noAccountYet: {
    en: 'No account exists yet. It is created after your WhatsApp number is verified.',
    hi: 'अभी कोई खाता नहीं बना है। यह आपका WhatsApp नंबर सत्यापित होने के बाद बनेगा।',
  },

  // --- validation ---
  errRequired: { en: 'This is needed to continue', hi: 'आगे बढ़ने के लिए यह ज़रूरी है' },
  errPhone: {
    en: 'Enter a 10-digit mobile number starting with 6, 7, 8 or 9',
    hi: '6, 7, 8 या 9 से शुरू होने वाला 10 अंकों का मोबाइल नंबर दर्ज करें',
  },
  errEmail: { en: 'Check this email address', hi: 'यह ईमेल पता जाँचें' },
  errConsent: { en: 'Accept this to continue', hi: 'आगे बढ़ने के लिए इसे स्वीकार करें' },
  errSummary: {
    en: 'Some details still need your attention.',
    hi: 'कुछ जानकारी पर ध्यान देना बाकी है।',
  },

  // --- step 3 (WhatsApp verify — this is what creates the account) ---
  s3title: { en: 'Verify your WhatsApp number', hi: 'अपना WhatsApp नंबर सत्यापित करें' },
  s3body: {
    en: 'Entering the correct code is what creates your account. There is no separate sign-up step after this.',
    hi: 'सही कोड दर्ज करना ही आपका खाता बनाता है। इसके बाद अलग से कोई साइन-अप चरण नहीं है।',
  },
  sendingTo: { en: 'Code goes to', hi: 'कोड जाएगा' },
  changeNumber: { en: 'Use a different number', hi: 'दूसरा नंबर इस्तेमाल करें' },
  sendCta: { en: 'Send the code on WhatsApp', hi: 'WhatsApp पर कोड भेजें' },
  sending: { en: 'Sending…', hi: 'भेज रहे हैं…' },
  sentNote: {
    en: 'Code sent to {phone}. It expires in 10 minutes.',
    hi: 'कोड {phone} पर भेजा गया। यह 10 मिनट में समाप्त हो जाएगा।',
  },
  codeLabel: { en: '6-digit code', hi: '6 अंकों का कोड' },
  createCta: { en: 'Confirm and create my account', hi: 'पुष्टि करें और मेरा खाता बनाएँ' },
  creating: { en: 'Creating your account…', hi: 'आपका खाता बना रहे हैं…' },
  resend: { en: 'Send the code again', hi: 'कोड दोबारा भेजें' },
  resendIn: { en: 'Send again in {s}s', hi: '{s} सेकंड में दोबारा भेजें' },
  errCodeInvalid: {
    en: 'That code is not right. Check the last WhatsApp message and try again.',
    hi: 'यह कोड सही नहीं है। आख़िरी WhatsApp संदेश देखकर फिर कोशिश करें।',
  },
  errCodeExpired: { en: 'That code has expired. Send a new one.', hi: 'यह कोड समाप्त हो गया है। नया भेजें।' },
  errCodeShort: { en: 'Enter all six digits', hi: 'सभी छह अंक दर्ज करें' },
  dupTitle: { en: 'An account already uses this number', hi: 'इस नंबर से पहले ही एक खाता है' },
  dupBody: {
    en: 'Sign in to that account instead, or use a different WhatsApp number. We stopped here so you do not repeat the verification steps.',
    hi: 'उस खाते में साइन इन करें, या दूसरा WhatsApp नंबर इस्तेमाल करें। हमने यहीं रोक दिया ताकि आपको सत्यापन दोबारा न करना पड़े।',
  },
  editNumber: { en: 'Edit the number above', hi: 'ऊपर दिया नंबर बदलें' },
  signIn: { en: 'Sign in', hi: 'साइन इन करें' },
  waDownTitle: {
    en: 'WhatsApp is not accepting messages right now — this is on our side',
    hi: 'WhatsApp अभी संदेश स्वीकार नहीं कर रहा — यह हमारी ओर की समस्या है',
  },
  waDownBody: {
    en: 'Your number is fine. Wait a moment and send the code again.',
    hi: 'आपका नंबर ठीक है। थोड़ा रुककर कोड दोबारा भेजें।',
  },
  s3foot: {
    en: 'Standard WhatsApp message rates may apply. We do not message your customers at this stage.',
    hi: 'सामान्य WhatsApp संदेश दरें लागू हो सकती हैं। इस चरण पर हम आपके ग्राहकों को कोई संदेश नहीं भेजते।',
  },
  rateLimited: {
    en: 'Too many attempts. Wait a few minutes and try again.',
    hi: 'बहुत अधिक प्रयास। कुछ मिनट रुककर फिर कोशिश करें।',
  },
  verifyUnavailable: {
    en: 'We could not verify that right now — this is on our side. Please try again.',
    hi: 'अभी सत्यापित नहीं कर सके — यह हमारी ओर से है। कृपया पुनः प्रयास करें।',
  },
  generic: {
    en: 'Something went wrong on our side. Try again.',
    hi: 'हमारी ओर कुछ गड़बड़ हो गई। फिर कोशिश करें।',
  },

  // --- step 4 (ownership review — proves nothing, says so) ---
  s4title: {
    en: 'Your account is ready. Start setting up your team.',
    hi: 'आपका खाता तैयार है। अपनी टीम तैयार करना शुरू करें।',
  },
  s4sub: {
    en: 'Four things make the biggest difference to how your agents work. You can do them now.',
    hi: 'चार चीज़ें आपके एजेंट के काम में सबसे बड़ा फ़र्क़ लाती हैं। आप उन्हें अभी कर सकते हैं।',
  },
  // Signup mints a PROOF token, not a session (verify-otp-for-signup: "signup has no
  // tenant ... issues a PROOF token instead of minting a session"), and the dashboard is
  // session-gated. So the honest next action is to sign in with the number just verified —
  // "Go to my workspace" would bounce every new owner to the login page.
  signInCta: { en: 'Sign in with this number', hi: 'इसी नंबर से साइन इन करें' },
  signInNote: {
    en: 'Use the WhatsApp number you just verified. We send a fresh code — there is no password.',
    hi: 'वही WhatsApp नंबर इस्तेमाल करें जो आपने अभी सत्यापित किया। हम नया कोड भेजेंगे — कोई पासवर्ड नहीं है।',
  },
  bannerTitle: {
    en: 'Your agents will not contact your customers yet',
    hi: 'आपके एजेंट अभी आपके ग्राहकों से संपर्क नहीं करेंगे',
  },
  bannerBody: {
    en: 'A person at Viabe reviews whether you own this business. Until that review finishes, every customer-facing action stays switched off. Your business is not verified yet.',
    hi: 'Viabe में एक व्यक्ति यह समीक्षा करता है कि यह व्यापार आपका है या नहीं। जब तक वह समीक्षा पूरी नहीं होती, ग्राहकों से जुड़ी हर कार्रवाई बंद रहती है। आपका व्यापार अभी सत्यापित नहीं है।',
  },
  reviewMeta: {
    en: 'We message you on WhatsApp when the review finishes, whichever way it goes.',
    hi: 'समीक्षा पूरी होने पर हम आपको WhatsApp पर बताते हैं — नतीजा जो भी हो।',
  },
  footer: {
    en: 'Viabe Team is a product of RKeCom Services (OPC) Pvt Ltd · Your data is stored in India',
    hi: 'Viabe Team, RKeCom Services (OPC) Pvt Ltd का एक उत्पाद है · आपका डेटा भारत में संग्रहीत होता है',
  },
  // The REAL next steps, from the onboarding wizard (VT-267 PR-C): review the drafted
  // business profile, then connect a data source and WhatsApp. The prototype invented a
  // different four (add products, describe how you sell, set a tone) that the product
  // does not ask for.
  td1: { en: 'Check the business profile we drafted', hi: 'हमने जो व्यापार प्रोफ़ाइल बनाई है उसे जाँचें' },
  td1n: {
    en: 'We build it from public records. Correct anything that is wrong before your agents use it.',
    hi: 'हम इसे सार्वजनिक रिकॉर्ड से बनाते हैं। एजेंट के इस्तेमाल से पहले जो ग़लत हो उसे सुधारें।',
  },
  td2: { en: 'Connect where your sales data lives', hi: 'जहाँ आपका बिक्री डेटा है उसे जोड़ें' },
  td2n: {
    en: 'Google Sheets or Shopify. Without it your agents have no customers to work with.',
    hi: 'Google Sheets या Shopify। इसके बिना आपके एजेंट के पास काम करने के लिए ग्राहक नहीं होंगे।',
  },
  td3: { en: 'Connect your WhatsApp Business number', hi: 'अपना WhatsApp Business नंबर जोड़ें' },
  td3n: {
    en: 'Connecting it does not send anything. Every send still waits for your approval.',
    hi: 'जोड़ने से कुछ भेजा नहीं जाता। हर संदेश आपकी मंज़ूरी का इंतज़ार करता है।',
  },
  back: { en: 'Back', hi: 'पीछे' },
} satisfies Record<string, Entry>

export type CopyKey = keyof typeof COPY

/** Resolve one key in the active language, with `{token}` substitution. */
export function t(lang: Lang, key: CopyKey, vars?: Record<string, string>): string {
  let s: string = COPY[key][lang] ?? COPY[key].en
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v)
  return s
}
