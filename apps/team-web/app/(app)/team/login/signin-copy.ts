/**
 * Bilingual copy for the owner sign-in flow, from the Claude Design "Viabe Reports"
 * project (`Signin Flow.dc.html`).
 *
 * The prototype's fixture behaviours are NOT carried over — it fakes "phone ending
 * 0000 = account exists", a three-wrong-codes lockout and a 15-minute pause, none of
 * which the server implements. Sign-in deliberately returns a GENERIC `{ sent: true }`
 * whether or not the number maps to a tenant, precisely so the page cannot leak tenant
 * existence; a "no account uses this number" screen would leak exactly that. So the
 * design's `noAcct` block is intentionally absent, and the copy below says the honest
 * thing instead: if the number has an account, a code is on its way.
 */

export type Lang = 'en' | 'hi'
type Entry = { en: string; hi: string }

export const SIGNIN_COPY = {
  langLabel: { en: 'Language', hi: 'भाषा' },
  h1: { en: 'Sign in to Viabe Team', hi: 'Viabe Team में साइन इन करें' },
  sub: {
    en: 'We send a code to the WhatsApp number on your account. There is no password.',
    hi: 'हम आपके खाते के WhatsApp नंबर पर एक कोड भेजते हैं। कोई पासवर्ड नहीं है।',
  },
  phoneLabel: { en: 'WhatsApp number', hi: 'WhatsApp नंबर' },
  phoneHint: {
    en: 'The number you verified when you created the account.',
    hi: 'वही नंबर जो खाता बनाते समय आपने सत्यापित किया था।',
  },
  sendCta: { en: 'Send the code on WhatsApp', hi: 'WhatsApp पर कोड भेजें' },
  sending: { en: 'Sending…', hi: 'भेज रहे हैं…' },
  noPassword: {
    en: 'We never ask for a password, an OTP over a call, or your GST login.',
    hi: 'हम कभी पासवर्ड, कॉल पर OTP, या आपका GST लॉगिन नहीं माँगते।',
  },
  errRequired: { en: 'Enter your WhatsApp number', hi: 'अपना WhatsApp नंबर दर्ज करें' },
  errPhone: {
    en: 'Enter a 10-digit mobile number starting with 6, 7, 8 or 9',
    hi: '6, 7, 8 या 9 से शुरू होने वाला 10 अंकों का मोबाइल नंबर दर्ज करें',
  },
  sendFailed: {
    en: 'Could not send a code. Try again.',
    hi: 'कोड नहीं भेज सके। फिर कोशिश करें।',
  },
  networkError: { en: 'Network error. Try again.', hi: 'नेटवर्क त्रुटि। फिर कोशिश करें।' },

  // code step
  sentTo: { en: 'Code sent to', hi: 'कोड भेजा गया' },
  sentGeneric: {
    en: 'If that number has an account, a code is on its way on WhatsApp.',
    hi: 'यदि उस नंबर से खाता है, तो WhatsApp पर कोड आ रहा है।',
  },
  editNumber: { en: 'Edit the number', hi: 'नंबर बदलें' },
  codeLabel: { en: '6-digit code', hi: '6 अंकों का कोड' },
  signInCta: { en: 'Sign in', hi: 'साइन इन करें' },
  signingIn: { en: 'Signing you in…', hi: 'साइन इन कर रहे हैं…' },
  errCodeShort: { en: 'Enter all six digits', hi: 'सभी छह अंक दर्ज करें' },
  errCodeGeneric: {
    en: 'That code did not work. Check the last WhatsApp message, or send a new code.',
    hi: 'यह कोड काम नहीं आया। आख़िरी WhatsApp संदेश देखें, या नया कोड भेजें।',
  },
  resend: { en: 'Send the code again', hi: 'कोड दोबारा भेजें' },
  expiry: { en: 'Codes expire in 10 minutes', hi: 'कोड 10 मिनट में समाप्त होते हैं' },
  newHere: { en: 'New to Viabe?', hi: 'Viabe पर नए हैं?' },
  createAccount: { en: 'Create an account', hi: 'नया खाता बनाएँ' },
  footer: {
    en: 'Viabe Technologies · DPDP-compliant · Data stored in India',
    hi: 'Viabe Technologies · DPDP अनुरूप · डेटा भारत में संग्रहीत',
  },
} satisfies Record<string, Entry>

export type SigninKey = keyof typeof SIGNIN_COPY

export function ts(lang: Lang, key: SigninKey): string {
  return SIGNIN_COPY[key][lang] ?? SIGNIN_COPY[key].en
}
