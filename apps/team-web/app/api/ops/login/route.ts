/**
 * VT-203 — Ops Console login: trigger Supabase magic-link.
 *
 * POST { email }
 *   → Supabase Auth signInWithOtp({ email, emailRedirectTo: callback })
 *   → 302 /team/ops/login?sent=1
 *
 * Per CL-421 (zero-paste): magic link is the user-facing flow; no
 * copy-paste secrets ever appear in the UI.
 */

import { NextResponse } from 'next/server'

import { serverSecretClient } from '@/lib/supabase-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(req: Request) {
  let email: string | undefined
  const ct = req.headers.get('content-type') ?? ''
  if (ct.includes('application/json')) {
    const body = (await req.json().catch(() => ({}))) as { email?: unknown }
    if (typeof body.email === 'string') email = body.email
  } else {
    const form = await req.formData().catch(() => null)
    const v = form?.get('email')
    if (typeof v === 'string') email = v
  }

  if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    return NextResponse.redirect(
      new URL('/team/ops/login?error=invalid_email', req.url),
      { status: 302 },
    )
  }

  const origin = new URL(req.url).origin
  const redirectTo = `${origin}/api/ops/login/callback`

  try {
    const supabase = serverSecretClient()
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: redirectTo },
    })
    if (error) {
      return NextResponse.redirect(
        new URL(`/team/ops/login?error=${encodeURIComponent(error.message)}`, req.url),
        { status: 302 },
      )
    }
  } catch (err) {
    return NextResponse.redirect(
      new URL(
        `/team/ops/login?error=${encodeURIComponent(
          err instanceof Error ? err.message : 'unknown',
        )}`,
        req.url,
      ),
      { status: 302 },
    )
  }

  return NextResponse.redirect(
    new URL('/team/ops/login?sent=1', req.url),
    { status: 302 },
  )
}
