import { type NextRequest, NextResponse } from 'next/server'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'

/**
 * VT-341 — owner monthly-report PDF download. GET (a download navigation is a legitimate
 * GET). tenant is derived SERVER-SIDE from the session (never a client field); year_month is
 * regex-validated HERE and again in the orchestrator (no path traversal). The orchestrator
 * mints a SHORT-TTL signed Storage URL for {SESSION_tenant}/{ym}.pdf; we 302-redirect to it.
 * A leaked/guessed URL cannot reach another tenant (the object path is the session tenant).
 */
const _base = () => process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
const _secret = () => process.env.INTERNAL_API_SECRET ?? ''
const _YEAR_MONTH = /^[0-9]{4}-(0[1-9]|1[0-2])$/

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ year_month: string }> },
): Promise<NextResponse> {
  const { year_month } = await ctx.params
  if (!_YEAR_MONTH.test(year_month)) {
    return NextResponse.json({ ok: false, reason: 'invalid_year_month' }, { status: 400 })
  }

  let tenantId: string
  try {
    ;({ tenantId } = await requireOwnerSession())
  } catch (err) {
    if (err instanceof OwnerUnauthorizedError) {
      return NextResponse.json({ ok: false, reason: 'unauthorized' }, { status: 401 })
    }
    throw err
  }

  let res: Response
  try {
    res = await fetch(`${_base()}/api/orchestrator/owner/report-download-url`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': _secret() },
      body: JSON.stringify({ tenant_id: tenantId, year_month }),
      signal: AbortSignal.timeout(10_000),
      cache: 'no-store',
    })
  } catch {
    return NextResponse.json({ ok: false, reason: 'orchestrator_unreachable' }, { status: 502 })
  }
  if (res.status === 404) return NextResponse.json({ ok: false }, { status: 404 })
  if (!res.ok) return NextResponse.json({ ok: false }, { status: 502 })

  const data = (await res.json()) as { signed_url?: string }
  if (!data.signed_url) return NextResponse.json({ ok: false }, { status: 404 })
  return NextResponse.redirect(data.signed_url, 302)
}
