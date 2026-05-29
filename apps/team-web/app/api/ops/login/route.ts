/**
 * VT-203 / VT-237 — Ops Console login.
 *
 * Two paths:
 *
 * 1. Env-password (VT-237 — Phase-1 single operator). POST with
 *    `password` field → constant-time compare against OPERATOR_PASSWORD
 *    + OPERATOR_EMAIL env. On match, mint operator JWT directly under
 *    FAZAL_OWNER_UUID and set the 7-day cookie via the shared helper.
 *    NO Supabase Auth round-trip. Avoids the magic-link email rate
 *    limit Fazal hit at 2026-05-29 21:25 IST.
 *
 * 2. Magic-link (VT-203). POST without `password` → Supabase
 *    `signInWithOtp` → callback verifies + sets cookie.
 *
 * Per CL-421: customer-facing tenant onboard at /team/onboard keeps
 * the zero-paste posture. VT-237 changes operator login only.
 */

import { timingSafeEqual } from 'node:crypto'
import { NextResponse } from 'next/server'

import { issueOperatorSessionRedirect } from '@/lib/auth/issue-operator-session'
import { serverSecretClient } from '@/lib/supabase-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

interface ParsedBody {
  email?: string
  password?: string
  next?: string
}

async function parseBody(req: Request): Promise<ParsedBody> {
  const out: ParsedBody = {}
  const ct = req.headers.get('content-type') ?? ''
  if (ct.includes('application/json')) {
    const body = (await req.json().catch(() => ({}))) as {
      email?: unknown
      password?: unknown
      next?: unknown
    }
    if (typeof body.email === 'string') out.email = body.email
    if (typeof body.password === 'string') out.password = body.password
    if (typeof body.next === 'string') out.next = body.next
    return out
  }
  const form = await req.formData().catch(() => null)
  const e = form?.get('email')
  if (typeof e === 'string') out.email = e
  const p = form?.get('password')
  if (typeof p === 'string') out.password = p
  const n = form?.get('next')
  if (typeof n === 'string') out.next = n
  return out
}

function constantTimeEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, 'utf8')
  const bBuf = Buffer.from(b, 'utf8')
  // Pad to equal length so timingSafeEqual doesn't throw on mismatched
  // length and leak the boundary. Length check stays in the same return
  // expression so the timing path is the same whether or not lengths match.
  const maxLen = Math.max(aBuf.length, bBuf.length)
  const aPadded = Buffer.concat([aBuf, Buffer.alloc(maxLen - aBuf.length)])
  const bPadded = Buffer.concat([bBuf, Buffer.alloc(maxLen - bBuf.length)])
  return timingSafeEqual(aPadded, bPadded) && aBuf.length === bBuf.length
}

async function handleEnvPasswordSignIn(
  email: string,
  password: string,
  rawNext: string | undefined,
  req: Request,
): Promise<NextResponse> {
  const expectedEmail = (process.env.OPERATOR_EMAIL ?? '').trim().toLowerCase()
  const expectedPassword = process.env.OPERATOR_PASSWORD ?? ''
  const operatorUuid = (process.env.FAZAL_OWNER_UUID ?? '').trim()

  if (!expectedEmail || !expectedPassword || !operatorUuid) {
    return NextResponse.redirect(
      new URL('/team/ops/login?error=password_login_not_configured', req.url),
      { status: 302 },
    )
  }

  const emailMatch = email.trim().toLowerCase() === expectedEmail
  const passwordMatch = constantTimeEqual(password, expectedPassword)
  if (!emailMatch || !passwordMatch) {
    return NextResponse.redirect(
      new URL('/team/ops/login?error=invalid_credentials', req.url),
      { status: 302 },
    )
  }

  return await issueOperatorSessionRedirect({
    operatorId: operatorUuid,
    rawNext: rawNext ?? null,
    requestUrl: req.url,
  })
}

async function handleMagicLink(
  email: string,
  rawNext: string | undefined,
  req: Request,
): Promise<NextResponse> {
  if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    return NextResponse.redirect(
      new URL('/team/ops/login?error=invalid_email', req.url),
      { status: 302 },
    )
  }

  const origin = new URL(req.url).origin
  const callbackUrl = new URL('/api/ops/login/callback', origin)
  if (rawNext) callbackUrl.searchParams.set('next', rawNext)
  const redirectTo = callbackUrl.toString()

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

export async function POST(req: Request) {
  const body = await parseBody(req)
  const email = body.email ?? ''

  if (typeof body.password === 'string' && body.password.length > 0) {
    return handleEnvPasswordSignIn(email, body.password, body.next, req)
  }
  return handleMagicLink(email, body.next, req)
}
