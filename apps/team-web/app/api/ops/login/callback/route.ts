/**
 * VT-203 — magic-link callback: verify Supabase session, mint operator
 * JWT, set HttpOnly cookie scoped to /team/ops, redirect to /team/ops.
 *
 * Cookie shape: viabe_ops_jwt — HttpOnly + Secure + SameSite=Lax +
 * path=/team/ops + 1h TTL (matches VT-188 token TTL).
 *
 * Per CL-421: end-to-end magic link; no copy-paste secrets in the flow.
 */

import { NextResponse } from 'next/server'

import { issueOperatorJwt } from '@/lib/auth/operator-jwt'
import { serverSecretClient } from '@/lib/supabase-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const COOKIE_TTL_SEC = 60 * 60  // 1 hour

export async function GET(req: Request) {
  const url = new URL(req.url)
  const code = url.searchParams.get('code')
  const tokenHash = url.searchParams.get('token_hash')
  const type = url.searchParams.get('type')

  if (!code && !tokenHash) {
    return NextResponse.redirect(
      new URL('/team/ops/login?error=missing_token', req.url),
      { status: 302 },
    )
  }

  try {
    const supabase = serverSecretClient()
    // Two callback shapes Supabase Auth uses:
    //   - PKCE / OAuth: ?code=...
    //   - OTP / magic link: ?token_hash=...&type=magiclink|email|recovery
    let userId: string | null = null
    if (code) {
      const { data, error } = await supabase.auth.exchangeCodeForSession(code)
      if (error || !data.session) {
        return NextResponse.redirect(
          new URL(
            `/team/ops/login?error=${encodeURIComponent(error?.message ?? 'no_session')}`,
            req.url,
          ),
          { status: 302 },
        )
      }
      userId = data.session.user.id
    } else if (tokenHash && type) {
      const { data, error } = await supabase.auth.verifyOtp({
        token_hash: tokenHash,
        // Supabase typing: magiclink/email/recovery; cast at boundary
        type: type as 'magiclink' | 'email' | 'recovery',
      })
      if (error || !data.session) {
        return NextResponse.redirect(
          new URL(
            `/team/ops/login?error=${encodeURIComponent(error?.message ?? 'no_session')}`,
            req.url,
          ),
          { status: 302 },
        )
      }
      userId = data.session.user.id
    }

    if (!userId) {
      return NextResponse.redirect(
        new URL('/team/ops/login?error=no_user_id', req.url),
        { status: 302 },
      )
    }

    // OPERATOR ALLOWLIST gate — Supabase Auth merely proves the email
    // was deliverable. Without this check, anyone with an email could
    // become an operator (privilege escalation). Phase 1 = single
    // operator (Fazal); compare against FAZAL_OWNER_UUID env. Multi-
    // operator (Phase 2) replaces this with an `operators` table
    // lookup or a Supabase app_metadata.role='operator' claim check.
    const operatorAllowlist = (process.env.FAZAL_OWNER_UUID ?? '').trim()
    if (!operatorAllowlist || userId !== operatorAllowlist) {
      return NextResponse.redirect(
        new URL('/team/ops/login?error=not_authorized', req.url),
        { status: 302 },
      )
    }

    const opJwt = await issueOperatorJwt(userId)
    const res = NextResponse.redirect(new URL('/team/ops', req.url), { status: 302 })
    res.cookies.set('viabe_ops_jwt', opJwt, {
      httpOnly: true,
      secure: true,
      sameSite: 'lax',
      path: '/team/ops',
      maxAge: COOKIE_TTL_SEC,
    })
    return res
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
}
