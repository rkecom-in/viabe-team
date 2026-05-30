/**
 * VT-250 — owner-portal login: PHONE ENTRY step.
 *
 * Owner enters their mobile → POST /api/team/auth/request-otp (Twilio Verify
 * over WhatsApp, the live channel) → advance to the code-entry step. The
 * response is intentionally generic ({ sent: true } regardless of whether the
 * phone maps to a tenant) so we never leak tenant existence here.
 *
 * Client component: the two-step flow (phone → code) is held in local state;
 * the actual auth happens server-side in the API routes.
 */

'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'

export default function OwnerLoginPage() {
  const router = useRouter()
  const [phone, setPhone] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const res = await fetch('/api/team/auth/request-otp', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string }
        setError(data.error ?? 'Could not send a code. Try again.')
        return
      }
      // Carry the entered phone to the code step (never persisted server-side
      // between steps — re-sent on verify). Encoded in the query string.
      router.push(`/team/login/code?phone=${encodeURIComponent(phone)}`)
    } catch {
      setError('Network error. Try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main
      className="min-h-screen flex items-center justify-center bg-gray-50 p-4"
      data-area="team-owner-login"
    >
      <div className="w-full max-w-md bg-white shadow-md rounded-lg p-8 space-y-6">
        <header className="text-center">
          <h1 className="text-2xl font-semibold text-gray-900">
            Sign in to Viabe
          </h1>
          <p className="text-sm text-gray-600 mt-2">
            Enter your mobile number — we&apos;ll send a code on WhatsApp.
          </p>
        </header>

        <form onSubmit={onSubmit} className="space-y-4" data-step="phone">
          <div>
            <label
              htmlFor="phone"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Mobile number
            </label>
            <input
              id="phone"
              name="phone"
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              required
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+91 98765 43210"
              className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            />
          </div>
          <button
            type="submit"
            disabled={submitting}
            data-element="send-code-button"
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white font-medium py-2 px-4 rounded-md shadow-sm transition-colors"
          >
            {submitting ? 'Sending…' : 'Send code'}
          </button>
        </form>

        {error ? (
          <p
            data-state="error"
            className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3"
          >
            {error}
          </p>
        ) : null}
      </div>
    </main>
  )
}
