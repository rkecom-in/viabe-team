'use client'

/**
 * VT-406 (Part B) — the signup entity-match sub-step (bilingual EN/HI). Sits AFTER business name +
 * city, BEFORE the OTP step. Flow: fetch UNVERIFIED candidates → owner picks one (or "none of
 * these") → confirm the picked GSTIN → render the verified result (authoritative registry name +
 * a "verified" chip) OR a graceful reject / retry.
 *
 * Provenance discipline (Fazal 2026-06-23): a candidate is "found" (web/GBP, UNCONFIRMED) — it shows
 * a "found" chip, NEVER "verified". Only a Sandbox-confirmed entity (status gstin_verified) shows the
 * "verified" chip + the authoritative name. A web/LLM field is never rendered as verified.
 *
 * The decision logic (fetch sequence, classify, gate) lives in lib/entity-match.ts so it's unit-
 * testable in the node env; this component is the thin bilingual presentation + sub-step transitions.
 */

import { useEffect, useState } from 'react'

import {
  canCreateAccount,
  classifyConfirm,
  confirmCandidate,
  fetchCandidates,
  isConfirmable,
  isValidGstinFormat,
  type EntityCandidate,
  type VerifiedEntity,
  type WizardStep,
} from '@/lib/entity-match'

type Lang = 'en' | 'hi'

type EmMsgKey =
  | 'heading' | 'subhead' | 'looking' | 'found_chip' | 'verified_chip'
  | 'source_web' | 'source_gbp' | 'no_gstin' | 'pick' | 'confirming'
  | 'none_of_these' | 'verified_heading' | 'verified_note' | 'continue'
  | 'reject_heading' | 'reject_body' | 'retry_heading' | 'retry_body' | 'try_again'
  | 'empty_candidates'
  | 'manual_with_gstin' | 'manual_heading' | 'manual_hint' | 'manual_label'
  | 'manual_placeholder' | 'manual_verify' | 'manual_format_error' | 'manual_not_registered'
  | 'manual_back'

const EM_MESSAGES: Record<Lang, Record<EmMsgKey, string>> = {
  en: {
    heading: 'Confirm your business',
    subhead: 'We found these. Pick yours so we work on the right business — or say none match.',
    looking: 'Looking up your business…',
    found_chip: 'Found',
    verified_chip: 'Verified',
    source_web: 'web',
    source_gbp: 'maps',
    no_gstin: 'No GST number found — can’t verify this one.',
    pick: 'This is mine',
    confirming: 'Verifying…',
    none_of_these: 'None of these match',
    verified_heading: 'Verified',
    verified_note: 'We confirmed your GST registration. This is the official registered name.',
    continue: 'Continue',
    // Generic terminus — SAME copy whether the GSTIN was inactive or simply not found (no oracle).
    reject_heading: 'We couldn’t verify a GST registration',
    reject_body: 'Viabe Team is for GST-registered businesses. We couldn’t confirm one for this business, so we can’t create an account right now.',
    retry_heading: 'Couldn’t check right now',
    retry_body: 'This is on our side — the verification service didn’t respond. Please try again in a moment.',
    try_again: 'Try again',
    empty_candidates: 'We couldn’t find your business in public records — you can enter your GST number to verify.',
    manual_with_gstin: 'Enter my GST number',
    manual_heading: 'Enter your GST number',
    manual_hint: 'We’ll verify it against the official GST registry.',
    manual_label: 'Your 15-character GSTIN',
    manual_placeholder: '22AAAAA0000A1Z5',
    manual_verify: 'Verify',
    manual_format_error: 'That doesn’t look like a valid 15-character GSTIN. Please check and re-enter.',
    manual_not_registered: 'I’m not GST-registered',
    manual_back: 'Back',
  },
  hi: {
    heading: 'अपना व्यवसाय पुष्टि करें',
    subhead: 'हमें ये मिले। अपना चुनें ताकि हम सही व्यवसाय पर काम करें — या बताएं कोई मेल नहीं खाता।',
    looking: 'आपका व्यवसाय खोजा जा रहा है…',
    found_chip: 'मिला',
    verified_chip: 'सत्यापित',
    source_web: 'वेब',
    source_gbp: 'मैप्स',
    no_gstin: 'कोई GST नंबर नहीं मिला — इसे सत्यापित नहीं कर सकते।',
    pick: 'यह मेरा है',
    confirming: 'सत्यापित किया जा रहा है…',
    none_of_these: 'इनमें से कोई मेल नहीं खाता',
    verified_heading: 'सत्यापित',
    verified_note: 'हमने आपका GST पंजीकरण पुष्टि किया। यह आधिकारिक पंजीकृत नाम है।',
    continue: 'जारी रखें',
    reject_heading: 'हम GST पंजीकरण सत्यापित नहीं कर सके',
    reject_body: 'Viabe Team GST-पंजीकृत व्यवसायों के लिए है। हम इस व्यवसाय के लिए इसकी पुष्टि नहीं कर सके, इसलिए अभी खाता नहीं बना सकते।',
    retry_heading: 'अभी जांच नहीं कर सके',
    retry_body: 'यह हमारी ओर से है — सत्यापन सेवा ने जवाब नहीं दिया। कृपया थोड़ी देर में पुनः प्रयास करें।',
    try_again: 'पुनः प्रयास करें',
    empty_candidates: 'हमें सार्वजनिक रिकॉर्ड में आपका व्यवसाय नहीं मिला — आप सत्यापित करने के लिए अपना GST नंबर दर्ज कर सकते हैं।',
    manual_with_gstin: 'मेरा GST नंबर दर्ज करें',
    manual_heading: 'अपना GST नंबर दर्ज करें',
    manual_hint: 'हम इसे आधिकारिक GST रजिस्ट्री से सत्यापित करेंगे।',
    manual_label: 'आपका 15-अंकीय GSTIN',
    manual_placeholder: '22AAAAA0000A1Z5',
    manual_verify: 'सत्यापित करें',
    manual_format_error: 'यह एक मान्य 15-अंकीय GSTIN नहीं लगता। कृपया जांचें और पुनः दर्ज करें।',
    manual_not_registered: 'मैं GST-पंजीकृत नहीं हूं',
    manual_back: 'वापस',
  },
}

export function EntityMatchStep({
  businessName,
  city,
  lang,
  onVerified,
  onReject,
}: {
  businessName: string
  city: string
  lang: Lang
  /** Called when a gstin_verified confirm lands — the verified entity unlocks account-creation. */
  onVerified: (entity: VerifiedEntity) => void
  /** Called on the graceful terminus (none-of-these / not GST-registered). */
  onReject: () => void
}) {
  const t = EM_MESSAGES[lang]
  const [step, setStep] = useState<WizardStep>('idle')
  const [candidates, setCandidates] = useState<EntityCandidate[]>([])
  const [confirming, setConfirming] = useState<string | null>(null) // the gstin being confirmed
  const [verified, setVerified] = useState<VerifiedEntity | null>(null)
  const [manualGstin, setManualGstin] = useState('') // VT-448 manual-entry input
  const [manualError, setManualError] = useState(false) // client-side GSTIN format error

  // Step 1: fetch candidates on mount. The component mounts in 'idle' (the loading screen); the
  // async result flips to 'picking'. Fail-closed → empty list → the picking screen still renders
  // with the not-listed path (lookup never blocks signup). name/city are fixed for the wizard's
  // lifetime (props from the details step), so this runs once — no setState-in-effect cascade.
  useEffect(() => {
    let cancelled = false
    fetchCandidates(businessName, city).then((r) => {
      if (cancelled) return
      setCandidates(r.candidates)
      setStep('picking')
    })
    return () => {
      cancelled = true
    }
  }, [businessName, city])

  // The shared confirm spine — a GSTIN (from a pick OR manual entry) → Sandbox confirm → classify.
  // The Sandbox confirm is the AUTHORITATIVE gate for both paths (a manually-typed GSTIN is verified
  // exactly like a picked one — it is never self-asserted).
  async function confirmGstin(gstin: string) {
    setConfirming(gstin)
    try {
      const outcome = classifyConfirm(await confirmCandidate(gstin), gstin)
      if (outcome.kind === 'verified') {
        // Show the verified result (authoritative name + chip) FIRST; Continue bridges to OTP/create
        // via onVerified. No auto-advance — the owner sees the confirmed entity before proceeding.
        setVerified({ gstin: outcome.gstin, name: outcome.name })
        setStep('verified')
      } else if (outcome.kind === 'retry') {
        setStep('retry')
      } else {
        setStep('reject')
        onReject()
      }
    } finally {
      setConfirming(null)
    }
  }

  function pick(candidate: EntityCandidate) {
    const gstin = candidate.candidate_gstin
    if (!gstin) return // GBP-only candidate (no registry id) → the manual-GSTIN path (VT-448), guarded in render
    void confirmGstin(gstin)
  }

  // VT-448 — the manual-GSTIN path: discovery is thin, OR the owner's only match is a bare/closed GBP
  // listing with no GSTIN (e.g. RKeCom). The owner types their GSTIN; the Sandbox confirm stays the gate.
  function openManual() {
    setManualGstin('')
    setManualError(false)
    setStep('manual_gstin')
  }

  function submitManualGstin() {
    const gstin = manualGstin.trim().toUpperCase()
    if (!isValidGstinFormat(gstin)) {
      setManualError(true) // format typo → inline retry (NOT a reject; the format gate is not an oracle)
      return
    }
    setManualError(false)
    void confirmGstin(gstin)
  }

  function notRegistered() {
    // The honest terminus when the owner has no GSTIN — the SAME generic reject (no enumeration oracle).
    setStep('reject')
    onReject()
  }

  function retry() {
    setStep('picking')
  }

  const chip = (text: string, tone: 'found' | 'verified') => (
    <span
      className={
        tone === 'verified'
          ? 'rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-700'
          : 'rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700'
      }
    >
      {text}
    </span>
  )

  const card =
    'rounded-2xl border border-gray-200 bg-white p-6 shadow-sm sm:p-8'

  if (step === 'idle') {
    return (
      <section data-entity-step="loading" className={`mt-8 ${card}`}>
        <p className="text-sm leading-relaxed text-gray-600">{t.looking}</p>
      </section>
    )
  }

  if (step === 'verified' && verified) {
    return (
      <section data-entity-step="verified" className={`mt-8 ${card}`}>
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900">{t.verified_heading}</h2>
          {chip(t.verified_chip, 'verified')}
        </div>
        {/* The AUTHORITATIVE registry name — Sandbox, not the candidate's web/LLM name. */}
        <p data-verified-name className="mt-3 text-base font-medium text-gray-900">
          {verified.name ?? businessName}
        </p>
        <p className="mt-2 text-sm leading-relaxed text-gray-600">{t.verified_note}</p>
        <button
          type="button"
          data-entity-continue
          disabled={!canCreateAccount(verified)}
          onClick={() => onVerified(verified)}
          className="mt-5 rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t.continue}
        </button>
      </section>
    )
  }

  if (step === 'reject') {
    return (
      <section data-entity-step="reject" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.reject_heading}</h2>
        <p data-reject-body className="mt-3 text-sm leading-relaxed text-gray-600">
          {t.reject_body}
        </p>
      </section>
    )
  }

  if (step === 'retry') {
    return (
      <section data-entity-step="retry" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.retry_heading}</h2>
        <p data-retry-body className="mt-3 text-sm leading-relaxed text-gray-600">{t.retry_body}</p>
        <button
          type="button"
          data-entity-retry
          onClick={retry}
          className="mt-5 rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700"
        >
          {t.try_again}
        </button>
      </section>
    )
  }

  if (step === 'manual_gstin') {
    return (
      <section data-entity-step="manual" className={`mt-8 ${card}`}>
        <h2 className="text-lg font-semibold text-gray-900">{t.manual_heading}</h2>
        <p className="mt-1 text-sm leading-relaxed text-gray-600">{t.manual_hint}</p>
        <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="manual-gstin">
          {t.manual_label}
        </label>
        <input
          id="manual-gstin"
          data-manual-gstin-input
          type="text"
          autoCapitalize="characters"
          autoComplete="off"
          maxLength={15}
          value={manualGstin}
          onChange={(e) => {
            setManualGstin(e.target.value.toUpperCase())
            if (manualError) setManualError(false)
          }}
          placeholder={t.manual_placeholder}
          className="mt-1 w-full rounded-xl border border-gray-300 px-4 py-3 font-mono uppercase tracking-wide text-gray-900 outline-none focus:border-emerald-500"
        />
        {manualError && (
          <p data-manual-error className="mt-2 text-sm text-red-600">{t.manual_format_error}</p>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-manual-verify
            disabled={confirming !== null || manualGstin.trim() === ''}
            onClick={submitManualGstin}
            className="rounded-xl bg-emerald-600 px-5 py-3 font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {confirming !== null ? t.confirming : t.manual_verify}
          </button>
          <button
            type="button"
            data-manual-back
            disabled={confirming !== null}
            onClick={() => setStep('picking')}
            className="rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
          >
            {t.manual_back}
          </button>
        </div>
        <button
          type="button"
          data-manual-not-registered
          disabled={confirming !== null}
          onClick={notRegistered}
          className="mt-4 block text-sm text-gray-500 underline underline-offset-2 transition hover:text-gray-700 disabled:opacity-50"
        >
          {t.manual_not_registered}
        </button>
      </section>
    )
  }

  // step === 'picking'
  return (
    <section data-entity-step="picking" className={`mt-8 ${card}`}>
      <h2 className="text-lg font-semibold text-gray-900">{t.heading}</h2>
      <p className="mt-1 text-sm leading-relaxed text-gray-600">{t.subhead}</p>
      {candidates.length === 0 ? (
        <p data-entity-empty className="mt-4 text-sm leading-relaxed text-gray-600">
          {t.empty_candidates}
        </p>
      ) : (
        <ul className="mt-4 flex flex-col gap-3">
          {candidates.map((c, i) => {
            const confirmable = isConfirmable(c)
            const display = c.trade_name || c.legal_name || businessName
            return (
              <li
                key={`${c.candidate_gstin ?? c.trade_name ?? 'c'}-${i}`}
                data-candidate
                data-source={c.source}
                className="flex flex-col gap-2 rounded-xl border border-gray-200 p-4"
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-900">{display}</span>
                  {/* Provenance: web/GBP candidates are FOUND, never verified. */}
                  {chip(t.found_chip, 'found')}
                  <span className="text-xs text-gray-400">
                    {c.source === 'gbp' ? t.source_gbp : t.source_web}
                  </span>
                </div>
                {c.legal_name && c.legal_name !== display && (
                  <span className="text-sm text-gray-600">{c.legal_name}</span>
                )}
                {c.detail && <span className="text-xs text-gray-500">{c.detail}</span>}
                {confirmable ? (
                  <button
                    type="button"
                    data-candidate-pick
                    disabled={confirming !== null}
                    onClick={() => pick(c)}
                    className="mt-1 self-start rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {confirming === c.candidate_gstin ? t.confirming : t.pick}
                  </button>
                ) : (
                  <span data-no-gstin className="mt-1 text-xs text-gray-400">{t.no_gstin}</span>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {/* VT-448: "not listed / found-but-no-GSTIN" is no longer a dead end — it opens the manual-GSTIN
          path (the owner verifies by typing their GSTIN; "not registered" inside is the honest reject). */}
      <button
        type="button"
        data-entity-manual
        disabled={confirming !== null}
        onClick={openManual}
        className="mt-5 rounded-xl border border-gray-300 px-5 py-2.5 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
      >
        {t.manual_with_gstin}
      </button>
    </section>
  )
}
