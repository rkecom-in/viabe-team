/** VT-97 — proxy POST for waitlist capture → orchestrator /api/waitlist.
 *
 * Pre-launch interest capture (email + WhatsApp + consent). Inert-by-construction: 404s
 * everywhere until ENABLE_WAITLIST_CAPTURE=true is explicitly set — the CL-422 gate (no real
 * waitlist PII until VT-231 Mumbai prod + Fazal, exactly like ENABLE_PUBLIC_SIGNUP). The
 * `waitlist` launch mode renders the FORM on dev, but this proxy never collects there.
 */
import { NextResponse } from 'next/server'

import { trustedClientIp } from '@/lib/auth/client-ip'
import { checkWaitlistRateLimit } from '@/lib/auth/otp-rate-limit'

const BASE = process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
const _secret = (): string => process.env.INTERNAL_API_SECRET ?? ''

export async function POST(request: Request): Promise<Response> {
  // CL-422 dark gate: no real waitlist PII collected until ENABLE_WAITLIST_CAPTURE=true
  // (VT-231 + Fazal). A comment is not a gate; this is.
  if (process.env.ENABLE_WAITLIST_CAPTURE !== 'true') {
    return NextResponse.json({ detail: { code: 'not_enabled' } }, { status: 404 })
  }

  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  if (!body || typeof body !== 'object') {
    return NextResponse.json({ detail: { code: 'invalid' } }, { status: 400 })
  }

  // Per-IP throttle — a flood backstop (distinct-email spam). The orchestrator dedups + the
  // X-Internal-Secret gates the BYPASSRLS write; this caps the edge.
  if (!checkWaitlistRateLimit(trustedClientIp(request)).allowed) {
    return NextResponse.json({ detail: { code: 'rate_limited' } }, { status: 429 })
  }

  try {
    const res = await fetch(`${BASE}/api/waitlist`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': _secret() },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    return NextResponse.json(data, { status: res.status })
  } catch {
    return NextResponse.json({ detail: { code: 'upstream' } }, { status: 502 })
  }
}
