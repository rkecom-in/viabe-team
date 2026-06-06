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

  if (done) return <p className="waitlist-done">{labels.submitted}</p>

  return (
    <form className="waitlist-form" onSubmit={onSubmit}>
      <p className="waitlist-notice">{labels.notice}</p>
      <label>
        {labels.email}
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
      </label>
      <label>
        {labels.phone}
        <input
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          placeholder="+919876543210"
          inputMode="tel"
          required
        />
      </label>
      <label className="waitlist-consent">
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => setConsent(e.target.checked)}
        />
        {labels.consent}
      </label>
      {error && (
        <p className="signup-error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" disabled={submitting || !consent}>
        {labels.submit}
      </button>
    </form>
  )
}
