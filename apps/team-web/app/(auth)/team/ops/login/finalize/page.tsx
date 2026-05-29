'use client'

/**
 * VT-233 — client-side fragment handler.
 *
 * Supabase Auth implicit flow lands here with `#access_token=...` in
 * URL fragment. Server-side callback can never read fragments, so:
 *
 * 1. This client component reads `window.location.hash` post-mount
 * 2. POSTs `access_token` to `/api/ops/login/finalize-hash`
 * 3. Server validates via `supabase.auth.getUser(access_token)` +
 *    FAZAL_OWNER_UUID allowlist + mints operator JWT cookie
 * 4. Client navigates to `next` returned by server, or `/team/ops`
 *
 * Matches VT-232 login-card aesthetic.
 */

import { useEffect, useState } from 'react'

export default function FinalizePage() {
  const [status, setStatus] = useState<'loading' | 'error'>('loading')
  const [errorMsg, setErrorMsg] = useState<string>('')

  useEffect(() => {
    async function finalize() {
      const hash = typeof window !== 'undefined' ? window.location.hash : ''
      if (!hash || !hash.includes('access_token=')) {
        setStatus('error')
        setErrorMsg('no access_token in URL fragment')
        return
      }

      const params = new URLSearchParams(hash.slice(1))
      const accessToken = params.get('access_token')
      const refreshToken = params.get('refresh_token') ?? undefined
      if (!accessToken) {
        setStatus('error')
        setErrorMsg('access_token missing')
        return
      }

      // Pull next from search params (server-callback set it before
      // 302 to this page). URL.searchParams since this is client-side.
      const url = new URL(window.location.href)
      const nextParam = url.searchParams.get('next') ?? ''

      try {
        const res = await fetch('/api/ops/login/finalize-hash', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            access_token: accessToken,
            refresh_token: refreshToken,
            next: nextParam,
          }),
        })
        const body = (await res.json().catch(() => ({}))) as {
          ok?: boolean
          next?: string
          error?: string
        }
        if (res.ok && body.ok && body.next) {
          // Replace so the fragment-bearing URL doesn't linger in history.
          window.location.replace(body.next)
          return
        }
        setStatus('error')
        setErrorMsg(body.error ?? `HTTP ${res.status}`)
      } catch (err) {
        setStatus('error')
        setErrorMsg(err instanceof Error ? err.message : 'network error')
      }
    }
    void finalize()
  }, [])

  return (
    <main
      className="min-h-screen flex items-center justify-center bg-gray-50 p-4"
      data-area="team-ops-login-finalize"
    >
      <div className="w-full max-w-md bg-white shadow-md rounded-lg p-8 space-y-4 text-center">
        <h1 className="text-xl font-semibold text-gray-900">
          {status === 'loading' ? 'Finalizing sign-in…' : 'Sign-in error'}
        </h1>
        {status === 'loading' ? (
          <div className="flex items-center justify-center py-4">
            <span
              className="inline-block w-6 h-6 border-2 border-gray-300 border-t-blue-600 rounded-full animate-spin"
              aria-label="loading"
            />
          </div>
        ) : (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3">
            {errorMsg}
          </p>
        )}
      </div>
    </main>
  )
}
