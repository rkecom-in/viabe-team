'use client'

/** VT-97 — the waitlist capture form (waitlist launch mode). Email + WhatsApp + a purpose
 * notice + ONE mandatory consent (DPDP at collection — VT-97 #1) that structurally gates
 * submit. Posts to the dark-gated /api/team/waitlist proxy. Bilingual via the `labels` prop
 * (the server page resolves them from the team-landing dict). CL-390: no PII telemetry. */

import { useState } from 'react'

export interface WaitlistLabels {
  notice: string
  email: string
  phone: string
  consent: string
  submit: string
  submitted: string
  error: string
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const PHONE_RE = /^\+91[6-9]\d{9}$/

/** Exported for node-env unit tests (the form itself needs a DOM). */
export function waitlistFieldsValid(email: string, phone: string, consent: boolean): boolean {
  return EMAIL_RE.test(email) && PHONE_RE.test(phone) && consent
}

export function WaitlistForm({ labels }: { labels: WaitlistLabels }) {
  const [email, setEmail] = useState('')
  const [phone, setPhone] = useState('')
  const [consent, setConsent] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!waitlistFieldsValid(email, phone, consent)) {
      setError(labels.error)
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch('/api/team/waitlist', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email, whatsapp_e164: phone, consent }),
      })
      if (res.ok) {
        setDone(true)
        return
      }
      setError(labels.error)
    } catch {
      setError(labels.error)
    } finally {
      setSubmitting(false)
    }
  }

  if (done)
    return (
      <p className="waitlist-done rounded-xl bg-emerald-50 px-5 py-3 font-medium text-emerald-800">
        {labels.submitted}
      </p>
    )

  return (
    <form
      className="waitlist-form flex w-full max-w-md flex-col gap-4 rounded-2xl border border-gray-200 bg-white p-6 text-left shadow-sm"
      onSubmit={onSubmit}
    >
      <p className="waitlist-notice text-sm text-gray-600">{labels.notice}</p>
      <label className="flex flex-col gap-1 text-sm font-medium text-gray-800">
        {labels.email}
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          className="rounded-lg border border-gray-300 px-3 py-2 text-base font-normal text-gray-900 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-100"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm font-medium text-gray-800">
        {labels.phone}
        <input
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          placeholder="+919876543210"
          inputMode="tel"
          required
          className="rounded-lg border border-gray-300 px-3 py-2 text-base font-normal text-gray-900 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-100"
        />
      </label>
      <label className="waitlist-consent flex items-start gap-2 text-sm text-gray-600">
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => setConsent(e.target.checked)}
          className="mt-0.5 h-4 w-4 accent-emerald-600"
        />
        {labels.consent}
      </label>
      {error && (
        <p className="signup-error text-sm font-medium text-red-600" role="alert">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={submitting || !consent}
        className="rounded-xl bg-emerald-600 px-5 py-2.5 font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-gray-300"
      >
        {labels.submit}
      </button>
    </form>
  )
}
