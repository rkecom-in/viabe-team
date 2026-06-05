import { type NextRequest, NextResponse } from 'next/server'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'

/**
 * VT-341 — owner self-serve DSR EXPORT proxy. POST only (a GET could be fired by a
 * prefetch/crawler/link-preview → an unintended full-PII export). tenant is derived
 * SERVER-SIDE from the owner session (never a client field — IDOR); the orchestrator scrubs
 * PII per its denylist. CSRF: the session cookie is SameSite=Strict; we also enforce
 * same-origin as a belt. (Self-serve DELETE is intentionally NOT wired — Fazal ruling
 * 2026-06-06; the orchestrator admin delete is Fazal/ops-only out-of-band.)
 */
const _base = () => process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
const _secret = () => process.env.INTERNAL_API_SECRET ?? ''

export async function POST(req: NextRequest): Promise<NextResponse> {
  // CSRF belt: reject a cross-origin POST even if a cookie somehow rode along.
  const origin = req.headers.get('origin')
  const host = req.headers.get('host')
  if (origin && host && new URL(origin).host !== host) {
    return NextResponse.json({ ok: false, reason: 'cross_origin' }, { status: 403 })
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
    res = await fetch(`${_base()}/api/orchestrator/admin/dsr/export`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': _secret() },
      body: JSON.stringify({ tenant_id: tenantId }),
      signal: AbortSignal.timeout(30_000),
      cache: 'no-store',
    })
  } catch {
    return NextResponse.json({ ok: false, reason: 'orchestrator_unreachable' }, { status: 502 })
  }
  if (res.status === 403) return NextResponse.json({ ok: false }, { status: 403 })
  if (!res.ok) return NextResponse.json({ ok: false }, { status: 502 })

  const buf = await res.arrayBuffer()
  return new NextResponse(buf, {
    status: 200,
    headers: {
      'content-type': 'application/zip',
      'content-disposition': 'attachment; filename="viabe-data-export.zip"',
    },
  })
}
