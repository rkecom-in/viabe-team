/**
 * VT-201 PR-3 — GET /api/ops/banner.
 *
 * Returns the same `BannerCounts` shape `<StickyBanner>` consumes.
 * Used by `<StickyBannerLive>`'s 30s client poll. Server-side helper
 * already caches at 60s (`lib/ops/banner.ts`), so this route is a thin
 * pass-through with the requireFazal gate.
 */

import { NextResponse } from 'next/server'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchBannerCounts } from '@/lib/ops/banner'

export const dynamic = 'force-dynamic'

export async function GET(): Promise<Response> {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ error: 'unauthenticated' }, { status: 401 })
    }
    throw err
  }
  const counts = await fetchBannerCounts()
  return NextResponse.json(counts)
}
