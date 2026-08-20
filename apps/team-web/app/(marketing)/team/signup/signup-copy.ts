/**
 * Bilingual copy for the signup wizard, from the Claude Design prototype
 * ("Viabe Reports" project, `Signup Flow.dc.html`).
 *
 * THREE THINGS FROM THE PROTOTYPE ARE DELIBERATELY NOT HERE, because shipping them
 * would mean shipping invented facts:
 *
 *  1. `c1body` / `c2body` — the prototype writes substantive DPDP and residency
 *     disclosure text ("You are the data fiduciary for your customers…"). That is
 *     legal copy and it is not ours to author. The expandable disclosure structure is
 *     built; the body is sourced from `disclosureBody` below, which is empty until
 *     legal copy exists. See NEEDS-FAZAL in the form.
 *  2. `c1version` / `c2version` — the prototype invents "Disclosure v2.3 · 14 Jan
 *     2026". The real identifiers are server-owned in
 *     `apps/team-orchestrator/config/disclosure_versions.yaml` and are what actually
 *     gets written to `consent_records`. They are surfaced from the server, never
 *     typed here, so the line the owner reads can never drift from the row we store.
 *  3. `bt1`–`bt10` — the prototype invents a business-type list. The real taxonomy is
 *     server-owned (`/api/team/business-types`, `label_en` / `label_hi`) and is
 *     constrained so the L3 k-anon cohorts stay populated (VT-82).
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
  uiLang: { en: 'Interface language', hi: 'इंटरफ़ेस की भाषा' },
  uiLangHint: {
    en: 'Saved to your account. You can change it later.',
    hi: 'आपके खाते में सहेजा जाएगा। बाद में बदल सकते हैं।',
  },

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
  disclosurePending: {
    en: 'The full disclosure text is not published yet. The version recorded with your consent is shown above.',
    hi: 'पूरा प्रकटीकरण अभी प्रकाशित नहीं हुआ है। आपकी सहमति के साथ दर्ज होने वाला संस्करण ऊपर दिखाया गया है।',
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
  workspaceCta: { en: 'Go to my workspace', hi: 'मेरे वर्कस्पेस पर जाएँ' },
  bannerTitle: {
    en: 'Your agents will not contact your customers yet',
    hi: 'आपके एजेंट अभी आपके ग्राहकों से संपर्क नहीं करेंगे',
  },
  bannerBody: {
    en: 'A person at Viabe reviews whether you own this business. Until that review finishes, every customer-facing action stays switched off. Your business is not verified yet.',
    hi: 'Viabe में एक व्यक्ति यह समीक्षा करता है कि यह व्यापार आपका है या नहीं। जब तक वह समीक्षा पूरी नहीं होती, ग्राहकों से जुड़ी हर कार्रवाई बंद रहती है। आपका व्यापार अभी सत्यापित नहीं है।',
  },
  reviewMeta: {
    en: 'Reviews usually finish within one working day. We message you on WhatsApp either way.',
    hi: 'समीक्षा आमतौर पर एक कार्यदिवस में पूरी हो जाती है। नतीजा जो भी हो, हम आपको WhatsApp पर बताते हैं।',
  },
  footer: {
    en: 'Viabe Technologies · DPDP-compliant · Data stored in India',
    hi: 'Viabe Technologies · DPDP अनुरूप · डेटा भारत में संग्रहीत',
  },
  td1: { en: 'Add your products or services', hi: 'अपने उत्पाद या सेवाएँ जोड़ें' },
  td1n: { en: 'Names and prices are enough to begin.', hi: 'शुरू करने के लिए नाम और दाम काफ़ी हैं।' },
  td2: { en: 'Describe how you sell today', hi: 'बताएँ आप आज कैसे बेचते हैं' },
  td2n: { en: 'Walk-ins, calls, WhatsApp, or a mix.', hi: 'दुकान पर, कॉल से, WhatsApp से, या मिला-जुला।' },
  td3: { en: 'Connect your WhatsApp Business number', hi: 'अपना WhatsApp Business नंबर जोड़ें' },
  td3n: { en: 'Connecting it does not start any messages.', hi: 'जोड़ने से कोई संदेश शुरू नहीं होता।' },
  td4: { en: 'Set the tone your agents use', hi: 'एजेंट की भाषा का लहजा तय करें' },
  td4n: { en: 'Formal or friendly, in English or Hindi.', hi: 'औपचारिक या मित्रवत, अंग्रेज़ी या हिन्दी में।' },
  back: { en: 'Back', hi: 'पीछे' },
} satisfies Record<string, Entry>

export type CopyKey = keyof typeof COPY

/** Resolve one key in the active language, with `{token}` substitution. */
export function t(lang: Lang, key: CopyKey, vars?: Record<string, string>): string {
  let s: string = COPY[key][lang] ?? COPY[key].en
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v)
  return s
}
