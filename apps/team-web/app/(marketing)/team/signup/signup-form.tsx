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
 * existed). Styled with Tailwind utilities (the repo's system): light theme, Viabe brand tokens
 * (saffron primary / warm-cream background — see globals.css), mobile-first; copy/flow/validation
 * untouched. Semantic classes kept alongside.
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
  | 'verify_unavailable' | 'gst_reject' | 'vendor_down'

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
    verify_unavailable: 'Couldn’t verify right now — this is on our side. Please try again.',
    gst_reject: 'Viabe Team is for GST-registered businesses. We couldn’t confirm one, so we can’t create an account right now.',
    vendor_down: 'This is on our side — the verification service didn’t respond. Please try again in a moment.',
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
    verify_unavailable: 'अभी सत्यापित नहीं कर सके — यह हमारी ओर से है। कृपया पुनः प्रयास करें।',
    gst_reject: 'Viabe Team GST-पंजीकृत व्यवसायों के लिए है। हम इसकी पुष्टि नहीं कर सके, इसलिए अभी खाता नहीं बना सकते।',
    vendor_down: 'यह हमारी ओर से है — सत्यापन सेवा ने जवाब नहीं दिया। कृपया थोड़ी देर में पुनः प्रयास करें।',
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
  // VT-512: track whether the current error is a terminal GST-gate reject (gst_reject from
  // the create call). Used to suppress the GST-error block on the OTP screen when verifiedEntity
  // is already set — a stale or spurious gst_reject must never confuse an owner who passed GST.
  const [gstRejectError, setGstRejectError] = useState(false)
  const [done, setDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  // VT-96 + VT-406 + VT-411: a 4-step flow — details, then entity-match (confirm the GST-registered
  // business BEFORE creating an account — VT-406/VT-408 verify-then-create gate), then OTP-verify the
  // WhatsApp number (the VT-326 proof token; a direct POST would 401) which CREATES the tenant, then
  // OWNERSHIP (the tier-2 step — prove the owner CONTROLS the business via a DISTINCT OTP to the
  // discovered public number; a GST entity is real, not necessarily yours).
  //
  // VT-411 ORDERING (Cowork fix): ownership runs POST-create so the orchestrator flips
  // owner_channel_verified on the REAL tenant. A PRE-create ownership step would call the
  // orchestrator with tenant_id='' → `WHERE id=''` no-op → the flag never persists. So: GST verify
  // gates create (tier-1, VT-408); ownership is the tier-2 step right after create — it gates the
  // journey/dashboard, NOT the create row (the tenant already exists when ownership runs).
  const [step, setStep] = useState<'details' | 'entity' | 'verify' | 'ownership'>('details')
  const [otpCode, setOtpCode] = useState('')
  // VT-406: the Sandbox-verified entity (gstin + authoritative name + discovered public phone). null
  // until a gstin_verified confirm lands; it gates create + rides into the create payload, and its
  // `phone` is the VT-411 ownership-OTP target.
  const [verifiedEntity, setVerifiedEntity] = useState<VerifiedEntity | null>(null)
  // VT-411: the REAL tenant_id returned by the create (201). The POST-create ownership step targets
  // it so owner_channel_verified flips on the actual tenant (never a no-op pre-create '').
  const [tenantId, setTenantId] = useState<string | null>(null)
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

  // VT-406 → VT-326 bridge — fired ONLY after the entity-match step server-confirms a verified
  // entity. Record the verified entity (it gates create + rides into the create payload), then
  // request the personal-WhatsApp OTP and advance to the verify step (which CREATES the tenant).
  // Ownership is deferred to AFTER create (VT-411 ordering) so it targets the real tenant.
  async function onEntityVerified(entity: VerifiedEntity) {
    setVerifiedEntity(entity)
    if (step === 'verify' || step === 'ownership' || submitting) return // double-click guard
    setError(null)
    setGstRejectError(false)
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

  // VT-411 — fired ONLY after the POST-create ownership step proves owner_channel_verified on the
  // REAL tenant (a DISTINCT OTP to the discovered public business number, or DIN). The tenant already
  // exists (created at the verify step); ownership is the tier-2 gate on the journey, so this just
  // closes the wizard. No further server call here — the flag flipped server-side in the step.
  function onOwnershipVerified() {
    setDone(true)
  }

  // Step 2 — verify the OTP → receive the pre-tenant verified-number token → CREATE the tenant
  // with `Authorization: Bearer <token>`. Invalid vs expired are NOT distinguished (generic —
  // no enumeration). The token is threaded straight to the proxy; never logged (CL-390). On a 201
  // the create returns the new tenant_id (VT-411) — store it + advance to the POST-create ownership
  // step so owner_channel_verified flips on the REAL tenant.
  async function onVerifyAndCreate(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setGstRejectError(false)
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
      // VT-449: thread the owner-CONFIRMED registry CIN ('' when none confirmed) — the orchestrator's
      // SignupBody.cin drives the MCA-canonical name-match + persists tenant_mca_data; '' falls back
      // to the typed business_name (existing behavior). CIN is a public registry id (CL-390-safe).
      const r = await verifyOtpAndCreate(
        {
          ...form,
          preferred_language: lang,
          // VT-512: field must be `gstin` — the orchestrator's SignupBody field name. The old
          // `verified_gstin` key was ignored by Pydantic (default ""), causing every create to
          // fail with 422 invalid_gstin regardless of the entity-step verify result.
          gstin: verifiedEntity.gstin,
          verified_name: verifiedEntity.name,
          cin: verifiedEntity.cin ?? '',
        },
        otpCode.trim(),
      )
      if (r.ok) {
        // VT-411: the tenant now EXISTS. Advance to the POST-create ownership step (tier-2) ONLY with
        // a real tenant_id — it's what targets the orchestrator's owner_channel_verified flip. If the
        // create somehow returned no id (degraded), don't run an ownership step that would flip nothing
        // on tenant_id='' (the exact no-op this ordering fixes) — the tenant exists; complete the wizard
        // and let the in-app journey re-prompt ownership. The create row is never blocked on tier-2.
        if (r.tenantId) {
          setTenantId(r.tenantId)
          setStep('ownership')
        } else {
          setDone(true)
        }
        return
      }
      // Sweep #8/#11: the new error variants are surfaced here too. `gst_reject` (422) is a TERMINAL
      // GST-registered-only reject; `vendor_down` (503) and `verify_unavailable` (502) are RETRYABLE
      // "on our side" outages — the form keeps the verify step (the owner can resubmit). We prefer
      // the orchestrator's authored bilingual `message` (gate_copy()) when the create gate provided
      // one, falling back to the local copy; the failure is no longer collapsed to one generic.
      const map: Record<typeof r.error, string> = {
        rate_limited: t.rate_limited,
        invalid_code: t.invalid_code,
        verify_unavailable: t.verify_unavailable,
        duplicate: t.duplicate,
        gst_reject: t.gst_reject,
        vendor_down: t.vendor_down,
        generic: t.generic,
      }
      // VT-512: flag GST-gate terminal rejects so the OTP screen can suppress the block
      // when verifiedEntity is already set (entity verified but create re-verified freshly).
      setGstRejectError(r.error === 'gst_reject')
      setError(r.message ?? map[r.error])
    } catch {
      setError(t.generic)
    } finally {
      setSubmitting(false)
    }
  }

  if (done) {
    return (
      <main className="signup-success flex min-h-screen flex-col items-center justify-center bg-background px-5 text-center text-foreground antialiased">
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
          <span
            aria-hidden
            className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-secondary/10 text-2xl font-bold text-secondary"
          >
            ✓
          </span>
          <p className="mt-4 font-medium leading-relaxed text-foreground">{t.success}</p>
        </div>
      </main>
    )
  }

  const langBtn = (active: boolean) =>
    active
      ? 'rounded-full bg-primary px-4 py-1.5 font-semibold text-primary-foreground'
      : 'rounded-full border border-input px-4 py-1.5 text-muted-foreground transition hover:bg-muted'
  const fieldLabel = 'flex flex-col gap-1.5 text-sm font-medium text-foreground'
  const fieldInput =
    'rounded-lg border border-input bg-card px-3 py-2.5 text-base font-normal text-foreground outline-none transition focus:border-primary focus:ring-2 focus:ring-ring/20'

  return (
    <main className="signup min-h-screen bg-background px-5 py-10 text-foreground antialiased sm:py-16">
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
        <h1 className="mt-6 text-center text-2xl font-bold tracking-tight text-foreground sm:text-3xl">
          {t.title}
        </h1>
        {step === 'details' ? (
        <form
          onSubmit={onSubmitDetails}
          className="mt-8 flex flex-col gap-5 rounded-2xl border border-border bg-card p-6 shadow-sm sm:p-8"
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
        <label className="signup-consent flex items-start gap-2.5 text-sm font-normal leading-relaxed text-muted-foreground">
          <input
            type="checkbox"
            checked={form.consent_dpdpa}
            onChange={(e) => update('consent_dpdpa', e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
          />
          {t.consent_dpdpa}
          {/* NEEDS-FAZAL: link to the DPDP disclosure copy (dpdpa_v1_2026-06). */}
        </label>
        <label className="signup-consent flex items-start gap-2.5 text-sm font-normal leading-relaxed text-muted-foreground">
          <input
            type="checkbox"
            checked={form.consent_residency}
            onChange={(e) => update('consent_residency', e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
          />
          {t.consent_residency}
          {/* NEEDS-FAZAL: link to the residency disclosure copy (residency_v1_2026-06). */}
        </label>
        {error && (
          <p className="signup-error rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={submitting || !form.consent_dpdpa || !form.consent_residency}
          className="rounded-xl bg-primary px-5 py-3 font-semibold text-primary-foreground shadow-sm transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.send_code}
        </button>
      </form>
      ) : step === 'entity' ? (
      // VT-406 entity-match sub-step — confirm a GST-registered business before account-creation.
      // onVerified records the verified entity (the create-account gate) + requests the WhatsApp OTP
      // (→ verify step → create); onReject is the graceful "GST-registered only" terminus.
      <EntityMatchStep
        businessName={form.business_name}
        city={form.city}
        lang={lang}
        // Sweep #1/#6: thread the parent OTP-request error + in-flight state into the entity step so a
        // failed "Verified → Continue" OTP send is VISIBLE on the verified screen and the Continue
        // button reflects the in-flight/blocked state (no silent re-fire of the OTP request).
        error={error}
        submitting={submitting}
        onVerified={(entity) => void onEntityVerified(entity)}
        onReject={() => { /* reject is recoverable IN the child step machine (re-enter GST / re-search) — no parent create path */ }}
      />
      ) : step === 'verify' ? (
      <form
        onSubmit={onVerifyAndCreate}
        className="mt-8 flex flex-col gap-5 rounded-2xl border border-border bg-card p-6 shadow-sm sm:p-8"
      >
        <p className="text-sm leading-relaxed text-muted-foreground">{t.code_sent}</p>
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
        {/* VT-512: gst_reject is gated on !verifiedEntity — when the entity IS verified,
            the GST-gate block is suppressed so a stale/spurious gst_reject never confuses
            an owner who passed the entity step. Other errors (invalid_code, rate_limited,
            duplicate, generic) render regardless of verifiedEntity. */}
        {error && !(gstRejectError && verifiedEntity) && (
          <p className="signup-error rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={submitting}
          className="rounded-xl bg-primary px-5 py-3 font-semibold text-primary-foreground shadow-sm transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.verify_create}
        </button>
        <button
          type="button"
          onClick={() => {
            setStep('details')
            setOtpCode('')
            setError(null)
            setGstRejectError(false)
          }}
          className="rounded-xl border border-input px-5 py-2.5 font-medium text-foreground transition hover:bg-muted"
        >
          {t.change_number}
        </button>
      </form>
      ) : (
      // VT-411 ownership sub-step — runs POST-create (the tenant EXISTS now). Prove the owner CONTROLS
      // the business via a DISTINCT OTP to the discovered public business number (or DIN). The REAL
      // tenant_id (from the create 201) targets the orchestrator's owner_channel_verified flip — a
      // pre-create '' would be a no-op. The discovered phone rides from the verified candidate (null →
      // owner enters it). cin is unknown at signup (the orchestrator validates against the company) → ''.
      <OwnershipStep
        tenantId={tenantId ?? ''}
        publicPhone={verifiedEntity?.phone ?? null}
        businessName={verifiedEntity?.name ?? form.business_name}
        cin=""
        lang={lang}
        onVerified={onOwnershipVerified}
      />
      )}
      </div>
    </main>
  )
}
