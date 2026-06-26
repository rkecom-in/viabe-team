/**
 * VT-96 — owner signup form (bilingual EN/HI). Consumes VT-82 POST /api/signup
 * via the /api/team/signup proxy; business_type options from /api/team/business-types
 * (the orchestrator taxonomy — single source of truth).
 *
 * NEEDS-FAZAL / public-exposure: this form must NOT be linked/deployed publicly until
 * VT-326 (OTP-before-create + per-IP throttle) lands — the backend front door is
 * un-throttled with no proof-of-control of the number (flooding + squatting). The
 * page structures a phone field that the later OTP step hooks before submit.
 *
 * CL-390: NO PII (name / phone / city) in any analytics/telemetry event.
 *
 * VT-378: the form shipped markup-only like the landing did (VT-372 twin — no stylesheet ever
 * existed). Styled with Tailwind utilities (the repo's system): light theme, emerald accent,
 * mobile-first; copy/flow/validation untouched. Semantic classes kept alongside.
 */
'use client'

import { useEffect, useState } from 'react'

import type { VerifiedEntity } from '@/lib/entity-match'
import { requestSignupOtp, verifyOtpAndCreate } from '@/lib/signup-otp'

import { EntityMatchStep } from './entity-match-step'
import { OwnershipStep } from './ownership-step'

type Lang = 'en' | 'hi'
type BizType = { key: string; label_en: string; label_hi: string }
type MsgKey =
  | 'title' | 'business_name' | 'owner_name' | 'whatsapp_number' | 'city'
  | 'business_type' | 'language' | 'consent_dpdpa' | 'consent_residency'
  | 'submit' | 'invalid_phone' | 'required' | 'duplicate' | 'generic' | 'success'
  | 'send_code' | 'code_sent' | 'enter_code' | 'verify_create' | 'invalid_code'
  | 'rate_limited' | 'change_number'

const MESSAGES: Record<Lang, Record<MsgKey, string>> = {
  en: {
    title: 'Sign up for Viabe Team',
    business_name: 'Business name',
    owner_name: 'Your name',
    whatsapp_number: 'WhatsApp number (+91…)',
    city: 'City',
    business_type: 'Business type',
    language: 'Language',
    consent_dpdpa: 'I agree to the data-processing notice (DPDP).',
    consent_residency: 'I agree to data being stored in India.',
    submit: 'Create my account',
    invalid_phone: 'Enter a valid +91 mobile number.',
    required: 'Please fill all fields and accept both consents.',
    duplicate: 'This number is already registered.',
    generic: 'Something went wrong. Please try again.',
    success: 'Account created — check WhatsApp for your welcome message.',
    send_code: 'Send code',
    code_sent: 'We sent a code to your WhatsApp. Enter it below.',
    enter_code: 'WhatsApp code',
    verify_create: 'Verify & create account',
    invalid_code: 'That code is invalid or expired. Try again.',
    rate_limited: 'Too many attempts. Please wait a few minutes.',
    change_number: 'Change number',
  },
  hi: {
    title: 'Viabe Team के लिए साइन अप करें',
    business_name: 'व्यवसाय का नाम',
    owner_name: 'आपका नाम',
    whatsapp_number: 'WhatsApp नंबर (+91…)',
    city: 'शहर',
    business_type: 'व्यवसाय का प्रकार',
    language: 'भाषा',
    consent_dpdpa: 'मैं डेटा-प्रोसेसिंग सूचना (DPDP) से सहमत हूँ।',
    consent_residency: 'मैं डेटा भारत में संग्रहीत होने से सहमत हूँ।',
    submit: 'मेरा खाता बनाएं',
    invalid_phone: 'एक मान्य +91 मोबाइल नंबर दर्ज करें।',
    required: 'कृपया सभी फ़ील्ड भरें और दोनों सहमतियाँ स्वीकार करें।',
    duplicate: 'यह नंबर पहले से पंजीकृत है।',
    generic: 'कुछ गलत हुआ। कृपया पुनः प्रयास करें।',
    success: 'खाता बन गया — स्वागत संदेश के लिए WhatsApp देखें।',
    send_code: 'कोड भेजें',
    code_sent: 'हमने आपके WhatsApp पर एक कोड भेजा है। नीचे दर्ज करें।',
    enter_code: 'WhatsApp कोड',
    verify_create: 'सत्यापित करें और खाता बनाएं',
    invalid_code: 'यह कोड अमान्य या समाप्त है। फिर से कोशिश करें।',
    rate_limited: 'बहुत अधिक प्रयास। कृपया कुछ मिनट प्रतीक्षा करें।',
    change_number: 'नंबर बदलें',
  },
}

const PHONE_RE = /^\+91[6-9]\d{9}$/

export function SignupForm() {
  const [lang, setLang] = useState<Lang>('en')
  const [bizTypes, setBizTypes] = useState<BizType[]>([])
  const [form, setForm] = useState({
    business_name: '',
    owner_name: '',
    whatsapp_number: '',
    city: '',
    business_type: '',
    consent_dpdpa: false,
    consent_residency: false,
  })
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  // VT-96 + VT-406 + VT-411: a 4-step flow — details, then entity-match (confirm the GST-registered
  // business BEFORE creating an account — VT-406/VT-408 verify-then-create gate), then OWNERSHIP
  // (prove the owner CONTROLS the business via a DISTINCT OTP to the discovered public number — a GST
  // entity is real, not necessarily yours), then OTP-verify the WhatsApp number (the VT-326 proof
  // token; a direct POST would 401). Account-creation is unreachable without a server-confirmed
  // verified entity AND a proven owner channel.
  const [step, setStep] = useState<'details' | 'entity' | 'ownership' | 'verify'>('details')
  const [otpCode, setOtpCode] = useState('')
  // VT-406: the Sandbox-verified entity (gstin + authoritative name). null until a gstin_verified
  // confirm lands; it gates the transition to the OTP/create steps and rides into the create payload.
  const [verifiedEntity, setVerifiedEntity] = useState<VerifiedEntity | null>(null)
  const t = MESSAGES[lang]

  useEffect(() => {
    fetch('/api/team/business-types')
      .then((r) => r.json())
      .then((d) => setBizTypes(d.business_types ?? []))
      .catch(() => setBizTypes([]))
  }, [])

  function update<K extends keyof typeof form>(k: K, v: (typeof form)[K]) {
    setForm((f) => ({ ...f, [k]: v }))
  }

  // Step 1 — validate the details, then advance to the VT-406 entity-match step. The OTP is NOT
  // requested here: we confirm a GST-registered business FIRST (verify-then-create) and only send a
  // WhatsApp code to an owner who passes — never to a reject-bound one.
  function onSubmitDetails(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (
      !form.business_name ||
      !form.owner_name ||
      !form.city ||
      !form.business_type ||
      !form.consent_dpdpa ||
      !form.consent_residency
    ) {
      setError(t.required)
      return
    }
    if (!PHONE_RE.test(form.whatsapp_number)) {
      setError(t.invalid_phone)
      return
    }
    setStep('entity')
  }

  // VT-406 → VT-411 bridge — fired ONLY after the entity-match step server-confirms a verified
  // entity. Record the verified entity (it gates create + rides into the create payload), then
  // advance to the OWNERSHIP step: a verified GST entity proves the business is real, not that this
  // owner controls it (Fazal's bar). The personal-WhatsApp OTP is deferred to AFTER ownership proof.
  function onEntityVerified(entity: VerifiedEntity) {
    setVerifiedEntity(entity)
    if (step === 'ownership' || step === 'verify' || submitting) return // double-click guard
    setError(null)
    setStep('ownership')
  }

  // VT-411 → VT-326 bridge — fired ONLY after the ownership step proves owner_channel_verified (a
  // DISTINCT OTP to the discovered public business number, or DIN). NOW request the personal-WhatsApp
  // OTP and advance to the verify step. Account-creation stays unreachable until BOTH proofs land.
  async function onOwnershipVerified() {
    if (step === 'verify' || submitting) return // double-click guard on the Continue button
    setError(null)
    setSubmitting(true)
    try {
      const r = await requestSignupOtp(form.whatsapp_number)
      if (!r.ok) {
        setError(r.error === 'rate_limited' ? t.rate_limited : t.generic)
        return
      }
      setStep('verify')
    } catch {
      setError(t.generic)
    } finally {
      setSubmitting(false)
    }
  }

  // Step 2 — verify the OTP → receive the pre-tenant verified-number token → create the tenant
  // with `Authorization: Bearer <token>`. Invalid vs expired are NOT distinguished (generic —
  // no enumeration). The token is threaded straight to the proxy; never logged (CL-390).
  async function onVerifyAndCreate(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!otpCode.trim()) {
      setError(t.invalid_code)
      return
    }
    // VT-406 create-account gate: NEVER create without a server-confirmed verified entity. The UI
    // can't reach this step un-verified (the entity step gates the transition), but assert it here
    // too — defence-in-depth (the server hard-block is VT-408; this is the UI invariant).
    if (!verifiedEntity?.gstin) {
      setError(t.generic)
      return
    }
    setSubmitting(true)
    try {
      // Carry the verified entity into the create payload so the orchestrator anchors discovery to
      // the CONFIRMED entity (gstin + authoritative name), not the owner-typed name (VT-406 fix).
      const r = await verifyOtpAndCreate(
        {
          ...form,
          preferred_language: lang,
          verified_gstin: verifiedEntity.gstin,
          verified_name: verifiedEntity.name,
        },
        otpCode.trim(),
      )
      if (r.ok) {
        setDone(true)
        return
      }
      const map = {
        rate_limited: t.rate_limited,
        invalid_code: t.invalid_code,
        duplicate: t.duplicate,
        generic: t.generic,
      }
      setError(map[r.error])
    } catch {
      setError(t.generic)
    } finally {
      setSubmitting(false)
    }
  }

  if (done) {
    return (
      <main className="signup-success flex min-h-screen flex-col items-center justify-center bg-gray-50 px-5 text-center text-gray-900 antialiased">
        <div className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-8 shadow-sm">
          <span
            aria-hidden
            className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100 text-2xl font-bold text-emerald-700"
          >
            ✓
          </span>
          <p className="mt-4 font-medium leading-relaxed text-gray-900">{t.success}</p>
        </div>
      </main>
    )
  }

  const langBtn = (active: boolean) =>
    active
      ? 'rounded-full bg-emerald-600 px-4 py-1.5 font-semibold text-white'
      : 'rounded-full border border-gray-300 px-4 py-1.5 text-gray-600 transition hover:bg-gray-100'
  const fieldLabel = 'flex flex-col gap-1.5 text-sm font-medium text-gray-700'
  const fieldInput =
    'rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-base font-normal text-gray-900 outline-none transition focus:border-emerald-600 focus:ring-2 focus:ring-emerald-600/20'

  return (
    <main className="signup min-h-screen bg-gray-50 px-5 py-10 text-gray-900 antialiased sm:py-16">
      <div className="mx-auto w-full max-w-md">
        <div className="signup-lang flex justify-center gap-2 text-sm">
          <button
            type="button"
            onClick={() => setLang('en')}
            aria-pressed={lang === 'en'}
            className={langBtn(lang === 'en')}
          >
            English
          </button>
          <button
            type="button"
            onClick={() => setLang('hi')}
            aria-pressed={lang === 'hi'}
            className={langBtn(lang === 'hi')}
          >
            हिंदी
          </button>
        </div>
        <h1 className="mt-6 text-center text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl">
          {t.title}
        </h1>
        {step === 'details' ? (
        <form
          onSubmit={onSubmitDetails}
          className="mt-8 flex flex-col gap-5 rounded-2xl border border-gray-200 bg-white p-6 shadow-sm sm:p-8"
        >
        <label className={fieldLabel}>
          {t.business_name}
          <input
            value={form.business_name}
            onChange={(e) => update('business_name', e.target.value)}
            maxLength={200}
            required
            className={fieldInput}
          />
        </label>
        <label className={fieldLabel}>
          {t.owner_name}
          <input
            value={form.owner_name}
            onChange={(e) => update('owner_name', e.target.value)}
            maxLength={120}
            required
            className={fieldInput}
          />
        </label>
        <label className={fieldLabel}>
          {t.whatsapp_number}
          <input
            value={form.whatsapp_number}
            onChange={(e) => update('whatsapp_number', e.target.value)}
            placeholder="+919876543210"
            inputMode="tel"
            required
            className={fieldInput}
          />
        </label>
        <label className={fieldLabel}>
          {t.city}
          <input
            value={form.city}
            onChange={(e) => update('city', e.target.value)}
            maxLength={120}
            required
            className={fieldInput}
          />
        </label>
        <label className={fieldLabel}>
          {t.business_type}
          <select
            value={form.business_type}
            onChange={(e) => update('business_type', e.target.value)}
            required
            className={fieldInput}
          >
            <option value="" disabled>
              —
            </option>
            {bizTypes.map((b) => (
              <option key={b.key} value={b.key}>
                {lang === 'hi' ? b.label_hi : b.label_en}
              </option>
            ))}
          </select>
        </label>
        <label className="signup-consent flex items-start gap-2.5 text-sm font-normal leading-relaxed text-gray-600">
          <input
            type="checkbox"
            checked={form.consent_dpdpa}
            onChange={(e) => update('consent_dpdpa', e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-emerald-600"
          />
          {t.consent_dpdpa}
          {/* NEEDS-FAZAL: link to the DPDP disclosure copy (dpdpa_v1_2026-06). */}
        </label>
        <label className="signup-consent flex items-start gap-2.5 text-sm font-normal leading-relaxed text-gray-600">
          <input
            type="checkbox"
            checked={form.consent_residency}
            onChange={(e) => update('consent_residency', e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-emerald-600"
          />
          {t.consent_residency}
          {/* NEEDS-FAZAL: link to the residency disclosure copy (residency_v1_2026-06). */}
        </label>
        {error && (
          <p className="signup-error rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={submitting || !form.consent_dpdpa || !form.consent_residency}
          className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.send_code}
        </button>
      </form>
      ) : step === 'entity' ? (
      // VT-406 entity-match sub-step — confirm a GST-registered business before account-creation.
      // onVerified records the verified entity (the create-account gate) + advances to ownership;
      // onReject is the graceful "GST-registered only" terminus (no account offered).
      <EntityMatchStep
        businessName={form.business_name}
        city={form.city}
        lang={lang}
        onVerified={onEntityVerified}
        onReject={() => { /* terminal — the reject screen renders in-place; no create path */ }}
      />
      ) : step === 'ownership' ? (
      // VT-411 ownership sub-step — prove the owner CONTROLS the business via a DISTINCT OTP to the
      // discovered public business number (or DIN). A verified GST entity is real, not necessarily
      // theirs. onVerified bridges to the personal-WhatsApp OTP step. tenant_id is '' pre-create
      // (VT-408 ordering); the discovered phone rides from the verified candidate (null → owner enters
      // it). cin is unknown at signup (the orchestrator validates against the company) → ''.
      <OwnershipStep
        tenantId=""
        publicPhone={verifiedEntity?.phone ?? null}
        businessName={verifiedEntity?.name ?? form.business_name}
        cin=""
        lang={lang}
        onVerified={() => void onOwnershipVerified()}
      />
      ) : (
      <form
        onSubmit={onVerifyAndCreate}
        className="mt-8 flex flex-col gap-5 rounded-2xl border border-gray-200 bg-white p-6 shadow-sm sm:p-8"
      >
        <p className="text-sm leading-relaxed text-gray-600">{t.code_sent}</p>
        <label className={fieldLabel}>
          {t.enter_code}
          <input
            value={otpCode}
            onChange={(e) => setOtpCode(e.target.value)}
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            className={`${fieldInput} text-center text-lg tracking-[0.3em]`}
          />
        </label>
        {error && (
          <p className="signup-error rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={submitting}
          className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.verify_create}
        </button>
        <button
          type="button"
          onClick={() => {
            setStep('details')
            setOtpCode('')
            setError(null)
          }}
          className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50"
        >
          {t.change_number}
        </button>
      </form>
      )}
      </div>
    </main>
  )
}
