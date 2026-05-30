/**
 * VT-250 — owner-portal login: CODE ENTRY step.
 *
 * Owner enters the OTP delivered over WhatsApp → POST /api/team/auth/verify-otp
 * with { phone, code }. On approval the server resolves owner_phone → tenant,
 * mints the viabe_team_session cookie, and returns a redirect target. A denied
 * code, an unknown phone, or a verify error all return a generic failure.
 *
 * The phone is carried from the phone-entry step via the ?phone= query param
 * (re-sent on verify; never persisted server-side between steps).
 */

'use client'

import { Suspense, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'

function CodeEntryForm() {
  const router = useRouter()
  const params = useSearchParams()
  const phone = params.get('phone') ?? ''

  const [code, setCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const res = await fetch('/api/team/auth/verify-otp', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone, code }),
      })
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean
        redirect?: string
        error?: string
      }
      if (!res.ok || !data.ok) {
        setError(data.error ?? 'Invalid or expired code.')
        return
      }
      router.push(data.redirect ?? '/team/dashboard')
    } catch {
      setError('Network error. Try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="w-full max-w-md bg-white shadow-md rounded-lg p-8 space-y-6">
      <header className="text-center">
        <h1 className="text-2xl font-semibold text-gray-900">Enter your code</h1>
        <p className="text-sm text-gray-600 mt-2">
          We sent a 6-digit code on WhatsApp.
        </p>
      </header>

      <form onSubmit={onSubmit} className="space-y-4" data-step="code">
        <div>
          <label
            htmlFor="code"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Verification code
          </label>
          <input
            id="code"
            name="code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="123456"
            className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm tracking-widest text-center focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>
        <button
          type="submit"
          disabled={submitting}
          data-element="verify-code-button"
          className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white font-medium py-2 px-4 rounded-md shadow-sm transition-colors"
        >
          {submitting ? 'Verifying…' : 'Verify'}
        </button>
        <button
          type="button"
          onClick={() => router.push('/team/login')}
          className="w-full text-sm text-gray-600 hover:text-gray-900"
        >
          Use a different number
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
  )
}

export default function OwnerLoginCodePage() {
  return (
    <main
      className="min-h-screen flex items-center justify-center bg-gray-50 p-4"
      data-area="team-owner-login-code"
    >
      <Suspense fallback={null}>
        <CodeEntryForm />
      </Suspense>
    </main>
  )
}
