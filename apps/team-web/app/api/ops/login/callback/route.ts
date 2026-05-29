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
import { safeNext } from '@/lib/auth/safe-next'
import { serverSecretClient } from '@/lib/supabase-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

// VT-236: extended from 1h → 7d for single-operator phase-1 ergonomics.
// FAZAL_OWNER_UUID allowlist gate (VT-203 fix-2) keeps the threat surface
// equivalent — only Fazal's UUID issues a JWT.
const COOKIE_TTL_SEC = 60 * 60 * 24 * 7  // 7 days

export async function GET(req: Request) {
  const url = new URL(req.url)
  const code = url.searchParams.get('code')
  const tokenHash = url.searchParams.get('token_hash')
  const type = url.searchParams.get('type')

  if (!code && !tokenHash) {
    // VT-233: Supabase implicit flow lands here with the session in the
    // URL fragment (`#access_token=...`). Server-side routes can never
    // read fragments. Bounce to the client-side finalize page; preserve
    // the `?next=` allowlist so the client posts it back when it has the
    // token in hand.
    const nextParam = url.searchParams.get('next') ?? ''
    const finalize = new URL('/team/ops/login/finalize', req.url)
    if (nextParam) finalize.searchParams.set('next', nextParam)
    // The browser appends the fragment unchanged on a 302, so the
    // client sees `…/finalize?next=…#access_token=…` and can read it.
    return NextResponse.redirect(finalize, { status: 302 })
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

    // VT-233: open-redirect allowlist extracted to shared helper for reuse
    // by finalize-hash endpoint.
    const next = safeNext(url.searchParams.get('next'))

    const res = NextResponse.redirect(new URL(next, req.url), { status: 302 })
    // VT-230 cookie path widened from /team/ops → /team so the JWT is
    // sent on /team/onboard + /team/dashboard requests too. Stays out
    // of /api/* and / root per VT-203 LOCK 2.
    res.cookies.set('viabe_ops_jwt', opJwt, {
      httpOnly: true,
      secure: true,
      sameSite: 'lax',
      path: '/team',
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
