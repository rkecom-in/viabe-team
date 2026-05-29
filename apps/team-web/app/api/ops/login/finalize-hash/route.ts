/**
 * VT-233 — server-side handler for Supabase implicit-flow tokens.
 *
 * Client (finalize/page.tsx) reads the URL fragment, POSTs the
 * access_token here. We validate via `supabase.auth.getUser(access_token)`
 * (Supabase verifies JWT against its own JWKS), check FAZAL_OWNER_UUID
 * allowlist (VT-203 fix-2 security gate), mint operator JWT, set cookie.
 *
 * Returns JSON `{ok, next}` so the client can navigate.
 */

import { NextResponse } from 'next/server'

import { issueOperatorJwt } from '@/lib/auth/operator-jwt'
import { safeNext } from '@/lib/auth/safe-next'
import { serverSecretClient } from '@/lib/supabase-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

// VT-236: 1h → 7d for single-operator phase-1; allowlist gate intact.
const COOKIE_TTL_SEC = 60 * 60 * 24 * 7 // 7 days

export async function POST(req: Request) {
  let body: { access_token?: unknown; next?: unknown }
  try {
    body = (await req.json()) as typeof body
  } catch {
    return NextResponse.json({ ok: false, error: 'invalid_json' }, { status: 400 })
  }

  const accessToken = typeof body.access_token === 'string' ? body.access_token : ''
  if (!accessToken) {
    return NextResponse.json({ ok: false, error: 'missing_access_token' }, { status: 400 })
  }

  const next = safeNext(typeof body.next === 'string' ? body.next : null)

  let userId: string | null = null
  try {
    const supabase = serverSecretClient()
    const { data, error } = await supabase.auth.getUser(accessToken)
    if (error || !data?.user?.id) {
      return NextResponse.json(
        { ok: false, error: error?.message ?? 'invalid_token' },
        { status: 401 },
      )
    }
    userId = data.user.id
  } catch (err) {
    return NextResponse.json(
      { ok: false, error: err instanceof Error ? err.message : 'auth_error' },
      { status: 500 },
    )
  }

  // OPERATOR ALLOWLIST gate — same defense as VT-203 fix-2.
  const operatorAllowlist = (process.env.FAZAL_OWNER_UUID ?? '').trim()
  if (!operatorAllowlist || userId !== operatorAllowlist) {
    return NextResponse.json(
      { ok: false, error: 'not_authorized' },
      { status: 403 },
    )
  }

  const opJwt = await issueOperatorJwt(userId)
  const res = NextResponse.json({ ok: true, next })
  res.cookies.set('viabe_ops_jwt', opJwt, {
    httpOnly: true,
    secure: true,
    sameSite: 'lax',
    path: '/team',
    maxAge: COOKIE_TTL_SEC,
  })
  return res
}
