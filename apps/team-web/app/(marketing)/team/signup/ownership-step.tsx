'use client'

/**
 * VT-411 — the signup OWNERSHIP-verification sub-step (bilingual EN/HI). Runs POST-create (the
 * tier-2 model: gstin_verified gates create, owner_channel_verified flips right after) — the tenant
 * already exists, so this step targets the REAL tenant_id. Fazal's bar: a verified GST entity is not
 * the same as a proven OWNER — so here the owner proves they CONTROL the business by receiving a
 * DISTINCT OTP on the DISCOVERED PUBLIC business number (NOT the personal WhatsApp the signup OTP
 * already proved). A DIN verify is offered alongside (for directors / companies without a public number).
 *
 * Flow (OTP path): show "confirm you own [business] — we'll send a code to your public number" →
 * start → enter code → confirm → owner_channel_verified=true → onVerified(). When the discovered
 * `publicPhone` is absent, the owner confirms/enters the public number first. The DIN affordance
 * cross-links to an 8-digit DIN entry → din route.
 *
 * owner_channel_verified is the SOLE signal ownership is proven — a vendor failure NEVER fakes it
 * (every fetch fails CLOSED in lib/ownership-verify.ts). CL-390: no public_phone/din/cin/code logged.
 *
 * The decision logic (fetch sequence, format gates) lives in lib/ownership-verify.ts so it's unit-
 * testable in the node env; this component is the thin bilingual presentation + sub-step transitions.
 */

import { useState } from 'react'

import {
  confirmOwnershipOtp,
  isValidDinFormat,
  isValidPublicPhoneFormat,
  startOwnershipOtp,
  verifyOwnerViaDin,
} from '@/lib/ownership-verify'
// VT-448 — the "verify with your DIN instead" affordance is PARKED behind DIN_KYC_ENABLED (default
// OFF, Fazal 2026-06-26: Sandbox MCA/DIN is gov-unreliable). With it OFF, ownership is public-number
// OTP ONLY (Twilio, reliable). The DIN screen + verify path stay intact behind the flag.
import { DIN_KYC_ENABLED } from '@/lib/feature-flags'

type Lang = 'en' | 'hi'

/** The sub-step state machine. The component renders one screen per `step`. */
type OwnershipStep =
  | 'intro' // explain ownership-proof + (when no discovered number) capture the public number
  | 'code' // OTP dispatched to the public number; owner enters the code
  | 'din' // alternative: owner enters their 8-digit DIN
  | 'verified' // owner_channel_verified landed — onVerified bridges to create
  | 'error' // transient failure — show retry affordance

type OwMsgKey =
  | 'heading' | 'subhead' | 'phone_prefix' | 'send_code' | 'sending'
  | 'phone_label' | 'phone_placeholder' | 'phone_hint' | 'phone_format_error'
  | 'code_heading' | 'code_subhead' | 'code_label' | 'code_placeholder'
  | 'confirm' | 'confirming' | 'code_invalid' | 'resend' | 'change_phone'
  | 'din_cta' | 'din_heading' | 'din_subhead' | 'din_label' | 'din_placeholder'
  | 'din_format_error' | 'din_reason_label' | 'din_reason_placeholder'
  | 'din_verify' | 'din_verifying' | 'din_invalid' | 'din_back'
  | 'otp_back' | 'verified_heading' | 'verified_note' | 'continue'
  | 'error_heading' | 'error_body' | 'try_again'

const OW_MESSAGES: Record<Lang, Record<OwMsgKey, string>> = {
  en: {
    heading: 'Confirm you own this business',
    subhead:
      'A GST registration tells us the business is real — not that it’s yours. We’ll send a code to the public business number so we know you run it.',
    phone_prefix: 'We’ll send a code to',
    send_code: 'Send code to my business number',
    sending: 'Sending…',
    phone_label: 'Your public business number (+91…)',
    phone_placeholder: '+919876543210',
    phone_hint: 'We couldn’t find a public number — enter the one customers call.',
    phone_format_error: 'Enter a valid +91 business number.',
    code_heading: 'Enter the code we sent',
    code_subhead: 'We sent a code to your public business number. Enter it to confirm you own it.',
    code_label: 'Business-number code',
    code_placeholder: '••••••',
    confirm: 'Confirm ownership',
    confirming: 'Confirming…',
    code_invalid: 'That code is invalid or expired. Try again.',
    resend: 'Resend code',
    change_phone: 'Use a different number',
    din_cta: 'I’m a director — verify with my DIN instead',
    din_heading: 'Verify with your DIN',
    din_subhead:
      'If you’re a director of this company, enter your 8-digit DIN. We’ll check it against the company registry.',
    din_label: 'Your 8-digit DIN',
    din_placeholder: '01234567',
    din_format_error: 'A DIN is 8 digits. Please check and re-enter.',
    din_reason_label: 'Why DIN (optional)',
    din_reason_placeholder: 'e.g. no public number for this company',
    din_verify: 'Verify my DIN',
    din_verifying: 'Verifying…',
    din_invalid: 'We couldn’t verify that DIN against this company. Please check and try again.',
    din_back: 'Back to the code',
    otp_back: 'Back',
    verified_heading: 'Ownership confirmed',
    verified_note: 'We confirmed you control this business. You’re all set to finish signing up.',
    continue: 'Continue',
    error_heading: 'Couldn’t send the code right now',
    error_body: 'This is on our side — the verification service didn’t respond. Please try again in a moment.',
    try_again: 'Try again',
  },
  hi: {
    heading: 'पुष्टि करें कि यह व्यवसाय आपका है',
    subhead:
      'GST पंजीकरण बताता है कि व्यवसाय असली है — यह नहीं कि वह आपका है। हम सार्वजनिक व्यवसाय नंबर पर एक कोड भेजेंगे ताकि हमें पता चले कि आप इसे चलाते हैं।',
    phone_prefix: 'हम इस पर कोड भेजेंगे',
    send_code: 'मेरे व्यवसाय नंबर पर कोड भेजें',
    sending: 'भेजा जा रहा है…',
    phone_label: 'आपका सार्वजनिक व्यवसाय नंबर (+91…)',
    phone_placeholder: '+919876543210',
    phone_hint: 'हमें सार्वजनिक नंबर नहीं मिला — वह दर्ज करें जिस पर ग्राहक कॉल करते हैं।',
    phone_format_error: 'एक मान्य +91 व्यवसाय नंबर दर्ज करें।',
    code_heading: 'हमने भेजा कोड दर्ज करें',
    code_subhead:
      'हमने आपके सार्वजनिक व्यवसाय नंबर पर एक कोड भेजा है। पुष्टि करने के लिए इसे दर्ज करें कि यह आपका है।',
    code_label: 'व्यवसाय-नंबर कोड',
    code_placeholder: '••••••',
    confirm: 'स्वामित्व की पुष्टि करें',
    confirming: 'पुष्टि की जा रही है…',
    code_invalid: 'यह कोड अमान्य या समाप्त है। फिर से कोशिश करें।',
    resend: 'कोड फिर भेजें',
    change_phone: 'अलग नंबर उपयोग करें',
    din_cta: 'मैं निदेशक हूँ — इसके बजाय मेरे DIN से सत्यापित करें',
    din_heading: 'अपने DIN से सत्यापित करें',
    din_subhead:
      'यदि आप इस कंपनी के निदेशक हैं, तो अपना 8-अंकीय DIN दर्ज करें। हम इसे कंपनी रजिस्ट्री से जांचेंगे।',
    din_label: 'आपका 8-अंकीय DIN',
    din_placeholder: '01234567',
    din_format_error: 'DIN 8 अंकों का होता है। कृपया जांचें और पुनः दर्ज करें।',
    din_reason_label: 'DIN क्यों (वैकल्पिक)',
    din_reason_placeholder: 'जैसे इस कंपनी का कोई सार्वजनिक नंबर नहीं',
    din_verify: 'मेरा DIN सत्यापित करें',
    din_verifying: 'सत्यापित किया जा रहा है…',
    din_invalid: 'हम उस DIN को इस कंपनी से सत्यापित नहीं कर सके। कृपया जांचें और पुनः प्रयास करें।',
    din_back: 'कोड पर वापस',
    otp_back: 'वापस',
    verified_heading: 'स्वामित्व की पुष्टि हुई',
    verified_note: 'हमने पुष्टि की कि आप इस व्यवसाय को नियंत्रित करते हैं। आप साइन अप पूरा करने के लिए तैयार हैं।',
    continue: 'जारी रखें',
    error_heading: 'अभी कोड नहीं भेज सके',
    error_body: 'यह हमारी ओर से है — सत्यापन सेवा ने जवाब नहीं दिया। कृपया थोड़ी देर में पुनः प्रयास करें।',
    try_again: 'पुनः प्रयास करें',
  },
}

export function OwnershipStep({
  tenantId,
  publicPhone,
  businessName,
  cin,
  lang,
  onVerified,
}: {
  /** VT-411: the REAL tenant_id — this step runs POST-create (the tenant exists), so the orchestrator
   *  flips owner_channel_verified on the actual tenant (a pre-create '' would be a `WHERE id=''` no-op). */
  tenantId: string
  /** The DISCOVERED public business number (GBP candidate's `phone`); null/'' → owner enters it. */
  publicPhone: string | null
  businessName: string
  /** The company CIN (for the DIN registry check); '' when unknown — the orchestrator validates. */
  cin: string
  lang: Lang
  /** Called when owner_channel_verified lands — ownership is proven; the wizard finishes signup. */
  onVerified: () => void
}) {
  const t = OW_MESSAGES[lang]
  // When a discovered number exists we open straight on 'intro' with it pre-filled; otherwise the
  // owner enters it on the same screen before we can send. Either way 'intro' is the entry step.
  const discovered = (publicPhone ?? '').trim()
  const [step, setStep] = useState<OwnershipStep>('intro')
  const [phone, setPhone] = useState(discovered) // the target number (discovered OR owner-entered)
  const [phoneError, setPhoneError] = useState(false) // client-side phone format error
  const [code, setCode] = useState('')
  const [codeError, setCodeError] = useState(false) // confirm came back not-verified
  const [din, setDin] = useState('')
  const [dinReason, setDinReason] = useState('')
  const [dinError, setDinError] = useState<'format' | 'invalid' | null>(null)
  const [busy, setBusy] = useState(false) // any in-flight request (start/confirm/din)
  // Sweep #5/#13 — force the editable phone field on intro even when a number was discovered. Set by
  // the intro "Use a different number" override (a wrong DISCOVERED number must be correctable before
  // the first send) AND by the 'code'-step "change number" recovery (a wrong OTP-target — stale GBP
  // phone or owner typo — isn't a dead-end). A local boolean (not mutating the derived `hasDiscovered`
  // or clearing `phone`, which would discard the value the owner may want to lightly edit).
  const [editingPhone, setEditingPhone] = useState(false)

  // The number is "known" (discovered or already valid) → hide the entry field on intro UNLESS the
  // owner has chosen to override/edit it (sweep #5/#13).
  const hasDiscovered = discovered !== ''

  async function send() {
    const target = phone.trim()
    if (!isValidPublicPhoneFormat(target)) {
      setPhoneError(true)
      return
    }
    setPhoneError(false)
    setBusy(true)
    try {
      const r = await startOwnershipOtp(tenantId, target)
      if (r.ok) {
        setCode('')
        setCodeError(false)
        setStep('code')
      } else {
        setStep('error') // transient — DIN remains available from the error screen
      }
    } finally {
      setBusy(false)
    }
  }

  async function confirm() {
    if (!code.trim()) {
      setCodeError(true)
      return
    }
    setBusy(true)
    try {
      const r = await confirmOwnershipOtp(tenantId, phone.trim(), code.trim())
      if (r.ownerChannelVerified) {
        setStep('verified')
      } else {
        setCodeError(true) // invalid/expired — generic (no enumeration tell)
      }
    } finally {
      setBusy(false)
    }
  }

  async function submitDin() {
    const normalized = din.trim()
    if (!isValidDinFormat(normalized)) {
      setDinError('format')
      return
    }
    setDinError(null)
    setBusy(true)
    try {
      const r = await verifyOwnerViaDin(tenantId, normalized, cin, dinReason.trim())
      if (r.ownerChannelVerified) {
        setStep('verified')
      } else {
        setDinError('invalid')
      }
    } finally {
      setBusy(false)
    }
  }

  function openDin() {
    setDin('')
    setDinReason('')
    setDinError(null)
    setStep('din')
  }

  const card = 'rounded-2xl border border-border bg-card p-6 shadow-sm sm:p-8'
  const primaryBtn =
    'rounded-xl bg-primary px-5 py-3 font-semibold text-primary-foreground shadow-sm transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50'
  const ghostBtn =
    'rounded-xl border border-input px-5 py-2.5 font-medium text-foreground transition hover:bg-muted disabled:opacity-50'
  const linkBtn =
    'block text-sm text-muted-foreground underline underline-offset-2 transition hover:text-foreground disabled:opacity-50'
  const input =
    'mt-1 w-full rounded-xl border border-input bg-card px-4 py-3 text-foreground outline-none focus:border-primary'

  if (step === 'verified') {
    return (
      <section data-ownership-step="verified" className={`mt-8 ${card}`}>
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-foreground">{t.verified_heading}</h2>
          <span className="rounded-full bg-secondary/10 px-2 py-0.5 text-xs font-semibold text-secondary">
            ✓
          </span>
        </div>
        <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{t.verified_note}</p>
        <button type="button" data-ownership-continue onClick={onVerified} className={`mt-5 ${primaryBtn}`}>
          {t.continue}
        </button>
      </section>
    )
  }

  if (step === 'error') {
    return (
      <section data-ownership-step="error" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-foreground">{t.error_heading}</h2>
        <p data-ownership-error-body className="mt-3 text-sm leading-relaxed text-muted-foreground">
          {t.error_body}
        </p>
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-ownership-retry
            disabled={busy}
            onClick={() => void send()}
            className={primaryBtn}
          >
            {busy ? t.sending : t.try_again}
          </button>
          {/* VT-448: DIN parked behind DIN_KYC_ENABLED (default OFF) — OTP-only ownership. */}
          {DIN_KYC_ENABLED && (
            <button type="button" data-ownership-din-from-error disabled={busy} onClick={openDin} className={ghostBtn}>
              {t.din_cta}
            </button>
          )}
        </div>
      </section>
    )
  }

  if (step === 'din') {
    return (
      <section data-ownership-step="din" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-foreground">{t.din_heading}</h2>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t.din_subhead}</p>
        <label className="mt-4 block text-sm font-medium text-foreground" htmlFor="ownership-din">
          {t.din_label}
        </label>
        <input
          id="ownership-din"
          data-ownership-din-input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          maxLength={8}
          value={din}
          onChange={(e) => {
            setDin(e.target.value.replace(/\D/g, ''))
            if (dinError) setDinError(null)
          }}
          placeholder={t.din_placeholder}
          className={`${input} font-mono tracking-wide`}
        />
        {dinError === 'format' && (
          <p data-ownership-din-error className="mt-2 text-sm text-destructive">{t.din_format_error}</p>
        )}
        {dinError === 'invalid' && (
          <p data-ownership-din-invalid className="mt-2 text-sm text-destructive">{t.din_invalid}</p>
        )}
        <label className="mt-4 block text-sm font-medium text-foreground" htmlFor="ownership-din-reason">
          {t.din_reason_label}
        </label>
        <input
          id="ownership-din-reason"
          data-ownership-din-reason
          type="text"
          autoComplete="off"
          maxLength={200}
          value={dinReason}
          onChange={(e) => setDinReason(e.target.value)}
          placeholder={t.din_reason_placeholder}
          className={input}
        />
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-ownership-din-verify
            disabled={busy || din.trim() === ''}
            onClick={() => void submitDin()}
            className={primaryBtn}
          >
            {busy ? t.din_verifying : t.din_verify}
          </button>
          <button
            type="button"
            data-ownership-din-back
            disabled={busy}
            onClick={() => setStep('intro')}
            className={ghostBtn}
          >
            {t.din_back}
          </button>
        </div>
      </section>
    )
  }

  if (step === 'code') {
    return (
      <section data-ownership-step="code" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-foreground">{t.code_heading}</h2>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t.code_subhead}</p>
        <label className="mt-4 block text-sm font-medium text-foreground" htmlFor="ownership-code">
          {t.code_label}
        </label>
        <input
          id="ownership-code"
          data-ownership-code-input
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={code}
          onChange={(e) => {
            setCode(e.target.value)
            if (codeError) setCodeError(false)
          }}
          placeholder={t.code_placeholder}
          className={`${input} text-center text-lg tracking-[0.3em]`}
        />
        {codeError && (
          <p data-ownership-code-error className="mt-2 text-sm text-destructive">{t.code_invalid}</p>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-ownership-confirm
            disabled={busy || code.trim() === ''}
            onClick={() => void confirm()}
            className={primaryBtn}
          >
            {busy ? t.confirming : t.confirm}
          </button>
          <button
            type="button"
            data-ownership-resend
            disabled={busy}
            onClick={() => void send()}
            className={ghostBtn}
          >
            {t.resend}
          </button>
          {/* Sweep #13: "change number" recovery — a wrong OTP-target number (stale discovered GBP
              phone or owner typo) is no longer a dead-end. Return to intro in edit mode and clear the
              bad code; the intro editable phone field renders (editingPhone) so the owner can correct
              the number and re-send. */}
          <button
            type="button"
            data-ownership-change-phone
            disabled={busy}
            onClick={() => {
              setEditingPhone(true)
              setCode('')
              setCodeError(false)
              setStep('intro')
            }}
            className={ghostBtn}
          >
            {t.change_phone}
          </button>
        </div>
        {/* VT-448: DIN parked behind DIN_KYC_ENABLED (default OFF) — OTP-only ownership. */}
        {DIN_KYC_ENABLED && (
          <button
            type="button"
            data-ownership-din-from-code
            disabled={busy}
            onClick={openDin}
            className={`mt-4 ${linkBtn}`}
          >
            {t.din_cta}
          </button>
        )}
      </section>
    )
  }

  // step === 'intro'
  return (
    <section data-ownership-step="intro" className={`mt-8 ${card}`}>
      <h2 className="text-lg font-semibold text-foreground">{t.heading}</h2>
      <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{t.subhead}</p>
      {hasDiscovered && !editingPhone ? (
        <>
          <p data-ownership-phone className="mt-4 text-sm text-foreground">
            {t.phone_prefix} <span className="font-mono font-medium text-foreground">{discovered}</span>
          </p>
          {/* Sweep #5: a wrong DISCOVERED number must be correctable BEFORE the first send. Reveal the
              editable phone input (pre-populated with the discovered value, still in `phone`) so the
              owner can override it; isValidPublicPhoneFormat already gates send(). */}
          <button
            type="button"
            data-ownership-edit-phone
            disabled={busy}
            onClick={() => {
              setEditingPhone(true)
              setPhoneError(false)
            }}
            className={`mt-2 ${linkBtn}`}
          >
            {t.change_phone}
          </button>
        </>
      ) : (
        <>
          <label className="mt-4 block text-sm font-medium text-foreground" htmlFor="ownership-phone">
            {t.phone_label}
          </label>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{t.phone_hint}</p>
          <input
            id="ownership-phone"
            data-ownership-phone-input
            type="tel"
            inputMode="tel"
            autoComplete="off"
            value={phone}
            onChange={(e) => {
              setPhone(e.target.value)
              if (phoneError) setPhoneError(false)
            }}
            placeholder={t.phone_placeholder}
            className={`${input} font-mono tracking-wide`}
          />
          {phoneError && (
            <p data-ownership-phone-error className="mt-2 text-sm text-destructive">{t.phone_format_error}</p>
          )}
        </>
      )}
      <div className="mt-5 flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-ownership-send
          disabled={busy || phone.trim() === ''}
          onClick={() => void send()}
          className={primaryBtn}
        >
          {busy ? t.sending : t.send_code}
        </button>
      </div>
      {/* DIN is offered alongside the OTP (Fazal's bar) — a director / company without a public number.
          VT-448: PARKED behind DIN_KYC_ENABLED (default OFF, Sandbox MCA/DIN unreliable) — when OFF,
          ownership is public-number OTP ONLY (Twilio). The DIN screen + verify path stay behind the flag. */}
      {DIN_KYC_ENABLED && (
        <button type="button" data-ownership-din-cta disabled={busy} onClick={openDin} className={`mt-4 ${linkBtn}`}>
          {t.din_cta}
        </button>
      )}
    </section>
  )
}
