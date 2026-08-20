/**
 * VT-96 — owner signup form (bilingual EN/HI). Consumes VT-82 POST /api/signup
 * via the /api/team/signup proxy.
 *
 * 2026-08-21 (Fazal): business type and city are NOT collected — they are auto-detected by
 * auto_discovery and confirmed in the onboarding journey. They are not in the create contract
 * either; SignupBody forbids extras, so sending one is a 422 rather than a silently ignored key.
 *
 * CL-390: NO PII (name / phone / city) in any analytics/telemetry event.
 *
 * 2026-08-21 — REDESIGNED to the Claude Design "Viabe Reports" project
 * (`Signup Flow.dc.html`). This changed the presentation only: every fetch, every
 * validation rule, the four-step order, the verify-then-create gate and both consent
 * semantics are the code that was already here. What is new is the shell (rail /
 * strip / progress by width), the Viabe type pairing, per-field inline errors in
 * place of one collapsed banner, and the expandable disclosure blocks.
 *
 * Deliberately NOT taken from the prototype: its invented disclosure body text and
 * invented version strings, and its invented business-type list. See signup-copy.ts.
 */
'use client'

import { useEffect, useRef, useState } from 'react'

import type { VerifiedEntity } from '@/lib/entity-match'
import { requestSignupOtp, verifyOtpAndCreate } from '@/lib/signup-otp'
import * as ui from '@/lib/viabe-ui'

import { EntityMatchStep } from './entity-match-step'
import { OwnershipStep } from './ownership-step'
import type { Lang } from './signup-copy'
import { t } from './signup-copy'
import { SignupLayout, type WizardStep } from './signup-shell'

/**
 * The identifiers actually written to `consent_records.{dpdpa,residency}_version`.
 * Server-owned in `apps/team-orchestrator/config/disclosure_versions.yaml` — mirrored
 * here so the owner reads the same string we store. If that file changes, this must
 * change with it; `signup-copy.ts` explains why the prototype's invented versions
 * ("Disclosure v2.3 · 14 Jan 2026") are not used.
 */
const DISCLOSURE_VERSION = {
  dpdpa: 'dpdpa_v1_2026-06',
  residency: 'residency_v1_2026-06',
} as const

const PHONE_RE = /^[6-9]\d{9}$/
// VT-724: optional email — validated only when non-empty (it routes the DPDP consent record).
const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/

type FieldErrors = Partial<
  Record<
    'business_name' | 'owner_name' | 'whatsapp_number' | 'owner_email' | 'preferred_language' | 'consent_dpdpa' | 'consent_residency',
    string
  >
>

export function SignupForm() {
  const [lang, setLang] = useState<Lang>('en')
  const [form, setForm] = useState({
    business_name: '',
    owner_name: '',
    whatsapp_number: '',
    owner_email: '',
    // The language the AGENTS message the owner in — an ASKED question, distinct from `lang`
    // (the header toggle, which is UI display only). Rides the create payload as
    // preferred_language and lands in tenants.preferred_language, the EXPLICIT choice column.
    preferred_language: '',
    consent_dpdpa: false,
    consent_residency: false,
  })
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [error, setError] = useState<string | null>(null)
  // VT-512: track whether the current error is a terminal GST-gate reject (gst_reject from
  // the create call). Used to suppress the GST-error block on the OTP screen when verifiedEntity
  // is already set — a stale or spurious gst_reject must never confuse an owner who passed GST.
  const [gstRejectError, setGstRejectError] = useState(false)
  const [done, setDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  // VT-96 + VT-406 + VT-517: a 4-step flow — details, then entity-match (confirm the GST-registered
  // business BEFORE creating an account — VT-406/VT-408 verify-then-create gate), then OTP-verify the
  // WhatsApp number (the VT-326 proof token; a direct POST would 401) which CREATES the tenant, then
  // OWNERSHIP — an honest "pending Viabe team review" screen (VT-517 killed self-serve ownership
  // OTP; a Viabe human decides ownership).
  const [step, setStep] = useState<WizardStep>('details')
  const [otpCode, setOtpCode] = useState('')
  // VT-406: the Sandbox-verified entity (gstin + authoritative name). null until a gstin_verified
  // confirm lands; it gates create + rides into the create payload.
  const [verifiedEntity, setVerifiedEntity] = useState<VerifiedEntity | null>(null)
  // VT-517: the REAL tenant_id returned by the create (201).
  const [tenantId, setTenantId] = useState<string | null>(null)
  const [openDisclosure, setOpenDisclosure] = useState<'dpdpa' | 'residency' | null>(null)
  const [resendIn, setResendIn] = useState(0)
  const resendTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => () => { if (resendTimer.current) clearInterval(resendTimer.current) }, [])

  function startResendCooldown() {
    if (resendTimer.current) clearInterval(resendTimer.current)
    setResendIn(30)
    resendTimer.current = setInterval(() => {
      setResendIn((n) => {
        if (n <= 1 && resendTimer.current) clearInterval(resendTimer.current)
        return n <= 1 ? 0 : n - 1
      })
    }, 1000)
  }

  function update<K extends keyof typeof form>(k: K, v: (typeof form)[K]) {
    setForm((f) => ({ ...f, [k]: v }))
    setFieldErrors((e) => ({ ...e, [k]: undefined }))
  }

  // Step 1 — validate the details, then advance to the VT-406 entity-match step. The OTP is NOT
  // requested here: we confirm a GST-registered business FIRST (verify-then-create) and only send a
  // WhatsApp code to an owner who passes — never to a reject-bound one.
  function onSubmitDetails(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    const errs: FieldErrors = {}
    if (!form.owner_name) errs.owner_name = t(lang, 'errRequired')
    if (!form.business_name) errs.business_name = t(lang, 'errRequired')
    if (!PHONE_RE.test(form.whatsapp_number)) errs.whatsapp_number = t(lang, 'errPhone')
    if (form.owner_email && !EMAIL_RE.test(form.owner_email.trim())) errs.owner_email = t(lang, 'errEmail')
    if (!form.preferred_language) errs.preferred_language = t(lang, 'errRequired')
    if (!form.consent_dpdpa) errs.consent_dpdpa = t(lang, 'errConsent')
    if (!form.consent_residency) errs.consent_residency = t(lang, 'errConsent')
    if (Object.keys(errs).length) {
      setFieldErrors(errs)
      setError(t(lang, 'errSummary'))
      return
    }
    setFieldErrors({})
    setStep('entity')
  }

  // VT-406 → VT-326 bridge — fired ONLY after the entity-match step server-confirms a verified
  // entity. Record the verified entity, request the personal-WhatsApp OTP, advance to verify.
  async function onEntityVerified(entity: VerifiedEntity) {
    setVerifiedEntity(entity)
    if (step === 'verify' || step === 'ownership' || submitting) return // double-click guard
    setError(null)
    setGstRejectError(false)
    setSubmitting(true)
    try {
      const r = await requestSignupOtp(form.whatsapp_number)
      if (!r.ok) {
        setError(r.error === 'rate_limited' ? t(lang, 'rateLimited') : t(lang, 'generic'))
        return
      }
      startResendCooldown()
      setStep('verify')
    } catch {
      setError(t(lang, 'generic'))
    } finally {
      setSubmitting(false)
    }
  }

  async function resendCode() {
    if (resendIn > 0 || submitting) return
    setError(null)
    try {
      const r = await requestSignupOtp(form.whatsapp_number)
      if (!r.ok) {
        setError(r.error === 'rate_limited' ? t(lang, 'rateLimited') : t(lang, 'generic'))
        return
      }
      setOtpCode('')
      startResendCooldown()
    } catch {
      setError(t(lang, 'generic'))
    }
  }

  // VT-517 — Continue on the pending-ownership-review screen. Ownership is NOT proven here.
  function onOwnershipVerified() {
    setDone(true)
  }

  // Step 3 — verify the OTP → receive the pre-tenant verified-number token → CREATE the tenant
  // with `Authorization: Bearer <token>`. Invalid vs expired are NOT distinguished (generic —
  // no enumeration). The token is threaded straight to the proxy; never logged (CL-390).
  async function onVerifyAndCreate(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setGstRejectError(false)
    if (otpCode.trim().length < 6) {
      setError(t(lang, 'errCodeShort'))
      return
    }
    // VT-406 create-account gate: NEVER create without a server-confirmed verified entity.
    if (!verifiedEntity?.gstin) {
      setError(t(lang, 'generic'))
      return
    }
    setSubmitting(true)
    try {
      const r = await verifyOtpAndCreate(
        {
          ...form,
          // VT-512: field must be `gstin` — the orchestrator's SignupBody field name.
          gstin: verifiedEntity.gstin,
          verified_name: verifiedEntity.name,
          cin: verifiedEntity.cin ?? '',
        },
        otpCode.trim(),
      )
      if (r.ok) {
        if (r.tenantId) {
          setTenantId(r.tenantId)
          setStep('ownership')
        } else {
          setDone(true)
        }
        return
      }
      const map: Record<typeof r.error, string> = {
        rate_limited: t(lang, 'rateLimited'),
        invalid_code: t(lang, 'errCodeInvalid'),
        verify_unavailable: t(lang, 'verifyUnavailable'),
        duplicate: t(lang, 'dupTitle'),
        gst_reject: t(lang, 'generic'),
        vendor_down: t(lang, 'waDownTitle'),
        generic: t(lang, 'generic'),
      }
      setGstRejectError(r.error === 'gst_reject')
      setError(r.message ?? map[r.error])
    } catch {
      setError(t(lang, 'generic'))
    } finally {
      setSubmitting(false)
    }
  }

  const phoneDisplay = `+91 ${form.whatsapp_number.slice(0, 5)}${form.whatsapp_number.length > 5 ? ' ' + form.whatsapp_number.slice(5) : ''}`

  // ---------------------------------------------------------------- rendering

  if (done) {
    return (
      <SignupLayout lang={lang} onLang={setLang} step="ownership">
        <section className={`${ui.panel} signup-success flex flex-col gap-4`}>
          <div className="flex items-center gap-2.5">
            <span className={ui.successDot} aria-hidden>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </span>
            <h2 className={ui.h2}>{t(lang, 's4title')}</h2>
          </div>
          <p className={ui.body}>{t(lang, 's4sub')}</p>
          {/* Signup issues a PROOF token, not a session, and the dashboard is
              session-gated — so send the owner to sign in rather than bounce them. */}
          <a href="/team/login" className={ui.primaryButton()}>
            {t(lang, 'signInCta')}
          </a>
          <p className={`${ui.hint} text-center`}>{t(lang, 'signInNote')}</p>
        </section>
        <p className={`${ui.hint} text-center`}>{t(lang, 'footer')}</p>
      </SignupLayout>
    )
  }

  const errorBlock = error && (
    <div className={`${ui.alertError} signup-error`} role="alert">
      <p className={ui.alertBody}>{error}</p>
    </div>
  )

  const fieldError = (k: keyof FieldErrors) =>
    fieldErrors[k] ? (
      <span className="text-xs font-medium text-ink-destructive">{fieldErrors[k]}</span>
    ) : null

  // ---- step 2: entity match (its own component; presentation updated in place) ----
  if (step === 'entity') {
    return (
      <SignupLayout lang={lang} onLang={setLang} step="entity">
        <EntityMatchStep
          businessName={form.business_name}
          // No city is collected any more — it is auto-detected. Discovery treats it as an
          // optional search hint (EntityCandidatesBody defaults it to ""), so it narrows results
          // when known and is simply absent when not.
          city=""
          lang={lang}
          // Sweep #1/#6: thread the parent OTP-request error + in-flight state into the entity step so
          // a failed "Verified → Continue" OTP send is VISIBLE on the verified screen and the Continue
          // button reflects the in-flight/blocked state (no silent re-fire of the OTP request).
          error={error}
          submitting={submitting}
          onVerified={(entity) => void onEntityVerified(entity)}
          onReject={() => { /* reject is recoverable IN the child step machine (re-enter GST / re-search) — no parent create path */ }}
        />
      </SignupLayout>
    )
  }

  // ---- step 4: ownership review (post-create, proves nothing, says so) ----
  if (step === 'ownership') {
    return (
      <SignupLayout lang={lang} onLang={setLang} step="ownership">
        <OwnershipStep
          tenantId={tenantId ?? ''}
          businessName={verifiedEntity?.name ?? form.business_name}
          lang={lang}
          onVerified={onOwnershipVerified}
        />
      </SignupLayout>
    )
  }

  // ---- step 3: WhatsApp verification — this is what creates the account ----
  if (step === 'verify') {
    return (
      <SignupLayout lang={lang} onLang={setLang} step="verify">
        <section className={`${ui.panel} flex flex-col gap-4`}>
          <div className="flex flex-col gap-2">
            <h2 className={ui.h2}>{t(lang, 's3title')}</h2>
            <p className={ui.body}>{t(lang, 's3body')}</p>
          </div>

          <div className={`${ui.alertInfo} flex-row flex-wrap items-center justify-between`}>
            <span className="flex flex-col gap-0.5">
              <span className="text-xs text-muted-foreground">{t(lang, 'sendingTo')}</span>
              <span className={ui.monoValue}>{phoneDisplay}</span>
            </span>
            <button
              type="button"
              onClick={() => { setStep('details'); setOtpCode(''); setError(null) }}
              className={ui.linkButton()}
            >
              {t(lang, 'changeNumber')}
            </button>
          </div>

          {gstRejectError && !verifiedEntity && (
            <div className={ui.alertError} role="alert">
              <span className={ui.alertTitle}>{t(lang, 'generic')}</span>
            </div>
          )}

          <form onSubmit={onVerifyAndCreate} className="flex flex-col gap-4">
            <label className={ui.fieldLabel}>
              <span className={ui.fieldLabelText}>{t(lang, 'codeLabel')}</span>
              <input
                type="text"
                inputMode="numeric"
                maxLength={6}
                autoComplete="one-time-code"
                value={otpCode}
                onChange={(e) => { setOtpCode(e.target.value.replace(/\D/g, '').slice(0, 6)); setError(null) }}
                placeholder="––––––"
                className={ui.codeField(Boolean(error))}
              />
            </label>

            {errorBlock}

            <button type="submit" disabled={submitting} className={ui.primaryButton(submitting)}>
              {submitting ? t(lang, 'creating') : t(lang, 'createCta')}
            </button>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <button
                type="button"
                onClick={resendCode}
                disabled={resendIn > 0}
                className={ui.linkButton(resendIn > 0)}
              >
                {resendIn > 0 ? t(lang, 'resendIn', { s: String(resendIn) }) : t(lang, 'resend')}
              </button>
              <span className={ui.hint}>{t(lang, 'sentNote', { phone: phoneDisplay })}</span>
            </div>
          </form>

          <p className={ui.hint}>{t(lang, 's3foot')}</p>
        </section>
      </SignupLayout>
    )
  }

  // ---- step 1: details ----
  const consentBlock = (
    which: 'dpdpa' | 'residency',
    titleKey: 'c1title' | 'c2title',
    acceptKey: 'c1accept' | 'c2accept',
    checked: boolean,
    onChange: (v: boolean) => void,
    errKey: 'consent_dpdpa' | 'consent_residency',
  ) => (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-background p-4">
      <span className="font-display text-sm font-bold text-foreground">{t(lang, titleKey)}</span>
      <label className="signup-consent flex items-start gap-2.5 text-sm leading-relaxed text-foreground">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-0.5 h-4.5 w-4.5 shrink-0 accent-[hsl(var(--viabe-saffron))]"
        />
        <span>{t(lang, acceptKey)}</span>
      </label>
      {fieldError(errKey)}
      <div className="flex flex-wrap items-center gap-2">
        <span className={ui.hint}>
          {t(lang, 'recordedAs')}{' '}
          <span className="font-mono">{DISCLOSURE_VERSION[which]}</span>
        </span>
        <button
          type="button"
          onClick={() => setOpenDisclosure(openDisclosure === which ? null : which)}
          className={ui.linkButton()}
          aria-expanded={openDisclosure === which}
        >
          {openDisclosure === which ? t(lang, 'hideMore') : t(lang, 'readMore')}
        </button>
      </div>
      {openDisclosure === which && (
        <div className="flex flex-col gap-2 border-t border-border pt-2.5">
          {/* A factual summary of what this consent actually covers. The BINDING text is
              counsel's and lives on the linked page, which states its own draft status —
              we do not author legal copy here. */}
          <p className={ui.bodySm}>{t(lang, which === 'dpdpa' ? 'c1summary' : 'c2summary')}</p>
          <p className={`${ui.hint} italic`}>{t(lang, 'disclosureDraft')}</p>
          <a
            href={which === 'dpdpa' ? '/team/privacy' : '/team/dpdp'}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[13px] font-semibold text-ink-primary underline"
          >
            {t(lang, 'disclosureLink')}
          </a>
        </div>
      )}
    </div>
  )

  return (
    <SignupLayout lang={lang} onLang={setLang} step="details">
      <section className={`${ui.panel} signup flex flex-col gap-5`}>
        <p className={ui.body}>{t(lang, 's1sub')}</p>

        <form onSubmit={onSubmitDetails} className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className={ui.fieldLabel}>
              <span className={ui.fieldLabelText}>{t(lang, 'ownerName')}</span>
              <input
                value={form.owner_name}
                onChange={(e) => update('owner_name', e.target.value)}
                placeholder={t(lang, 'ownerNamePh')}
                autoComplete="name"
                className={ui.field(Boolean(fieldErrors.owner_name))}
              />
              {fieldError('owner_name')}
            </label>

            <label className={ui.fieldLabel}>
              <span className={ui.fieldLabelText}>{t(lang, 'businessName')}</span>
              <input
                value={form.business_name}
                onChange={(e) => update('business_name', e.target.value)}
                placeholder={t(lang, 'businessNamePh')}
                autoComplete="organization"
                className={ui.field(Boolean(fieldErrors.business_name))}
              />
              {fieldError('business_name')}
            </label>
          </div>

          {/* WhatsApp and email share a row on desktop, per the design; they stack below sm. */}
          <div className="grid gap-4 sm:grid-cols-2">
            <label className={ui.fieldLabel}>
              <span className={ui.fieldLabelText}>{t(lang, 'whatsapp')}</span>
              <span className="flex items-stretch">
                <span className={ui.phonePrefix}>+91</span>
                <input
                  type="tel"
                  inputMode="numeric"
                  value={form.whatsapp_number}
                  onChange={(e) => update('whatsapp_number', e.target.value.replace(/\D/g, '').slice(0, 10))}
                  placeholder="98765 43210"
                  autoComplete="tel-national"
                  className={ui.phoneField(Boolean(fieldErrors.whatsapp_number))}
                />
              </span>
              {fieldError('whatsapp_number')}
              <span className={ui.hint}>{t(lang, 'whatsappHint')}</span>
            </label>

            <label className={ui.fieldLabel}>
              <span className={ui.fieldLabelText}>{t(lang, 'email')}</span>
              <input
                type="email"
                value={form.owner_email}
                onChange={(e) => update('owner_email', e.target.value)}
                autoComplete="email"
                className={ui.field(Boolean(fieldErrors.owner_email))}
              />
              {fieldError('owner_email')}
              <span className={ui.hint}>{t(lang, 'emailHint')}</span>
            </label>
          </div>

          <fieldset className={ui.fieldLabel}>
            <legend className={ui.fieldLabelText}>{t(lang, 'commsLang')}</legend>
            <div className="mt-1.5 grid gap-2 sm:grid-cols-3">
              {([
                { v: 'en', label: t(lang, 'langEn'), eg: null },
                { v: 'hi', label: t(lang, 'langHi'), eg: null },
                { v: 'hinglish', label: t(lang, 'langHinglish'), eg: t(lang, 'langHinglishEg') },
              ] as const).map((o) => {
                const on = form.preferred_language === o.v
                return (
                  <label
                    key={o.v}
                    className={[
                      'flex cursor-pointer flex-col gap-1 rounded-xl border p-3 transition',
                      on ? 'border-primary bg-accent' : 'border-border bg-background hover:bg-muted',
                    ].join(' ')}
                  >
                    <span className="flex items-center gap-2">
                      <input
                        type="radio"
                        name="preferred_language"
                        value={o.v}
                        checked={on}
                        onChange={() => update('preferred_language', o.v)}
                        className="h-4 w-4 accent-[hsl(var(--viabe-saffron))]"
                      />
                      <span className="font-display text-sm font-bold text-foreground">{o.label}</span>
                    </span>
                    {o.eg && <span className="pl-6 text-xs leading-[1.5] text-muted-foreground">{o.eg}</span>}
                  </label>
                )
              })}
            </div>
            {fieldError('preferred_language')}
            <span className={ui.hint}>{t(lang, 'commsLangHint')}</span>
          </fieldset>

          <div className="flex flex-col gap-2.5 border-t border-border pt-4">
            <div className="flex flex-col gap-1">
              <span className="font-display text-[15px] font-bold text-foreground">{t(lang, 'consentH')}</span>
              <span className={ui.bodySm}>{t(lang, 'consentNote')}</span>
            </div>
            {consentBlock('dpdpa', 'c1title', 'c1accept', form.consent_dpdpa, (v) => update('consent_dpdpa', v), 'consent_dpdpa')}
            {consentBlock('residency', 'c2title', 'c2accept', form.consent_residency, (v) => update('consent_residency', v), 'consent_residency')}
          </div>

          {errorBlock}

          <button type="submit" disabled={submitting} className={ui.primaryButton(submitting)}>
            {t(lang, 'step1Cta')}
          </button>
          <p className={`${ui.hint} text-center`}>{t(lang, 'noAccountYet')}</p>
        </form>
      </section>
      <p className={`${ui.hint} text-center`}>{t(lang, 'footer')}</p>
    </SignupLayout>
  )
}
