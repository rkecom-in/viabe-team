/**
 * VT-201 PR-2 — historical pipeline_steps fetch endpoint.
 *
 * GET /api/ops/history?date=YYYY-MM-DD&hour=H&cursor=...&tenant_ids=a,b&step_kinds=...&statuses=...&q=...
 *
 * Auth: requireFazal() (Phase-1 single-operator). Multi-operator
 * Phase-2 will route via per-operator JWTs (CL-88 substrate).
 *
 * Keyset paginated; returns `{ rows, next_cursor }` per call. Client
 * pages through until next_cursor is null.
 */

import { NextResponse, type NextRequest } from 'next/server'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchHistoricalSteps } from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

export async function GET(req: NextRequest): Promise<Response> {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ error: 'unauthenticated' }, { status: 401 })
    }
    throw err
  }

  const sp = req.nextUrl.searchParams
  const date = sp.get('date')
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return NextResponse.json(
      { error: 'date param required (YYYY-MM-DD)' },
      { status: 400 },
    )
  }
  const hourParam = sp.get('hour')
  let hour: number | undefined
  if (hourParam !== null) {
    const h = Number(hourParam)
    if (!Number.isInteger(h) || h < 0 || h > 23) {
      return NextResponse.json({ error: 'invalid hour' }, { status: 400 })
    }
    hour = h
  }
  const cursor = sp.get('cursor') ?? null
  const limitParam = sp.get('limit')
  const limit = limitParam ? Math.min(Math.max(Number(limitParam) || 100, 1), 500) : 100

  const result = await fetchHistoricalSteps({
    date,
    hour,
    cursor,
    tenantIds: sp.get('tenant_ids')?.split(',').filter(Boolean),
    stepKinds: sp.get('step_kinds')?.split(',').filter(Boolean),
    statuses: sp.get('statuses')?.split(',').filter(Boolean),
    q: sp.get('q') ?? undefined,
    limit,
  })
  return NextResponse.json({
    rows: result.rows,
    next_cursor: result.nextCursor,
  })
}
