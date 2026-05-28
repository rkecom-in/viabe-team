import type { ReactNode } from 'react'

import { StickyBannerLive } from '@/components/ops/sticky-banner-live'
import { fetchBannerCounts } from '@/lib/ops/banner'

/**
 * Ops UI shell.
 *
 * VT-201 PR-3: hoists the sticky banner here so all `/team/ops/**` pages
 * render the last-24h operator-awareness counts without per-page wiring.
 * Auth-gating happens at child-page level (each page calls requireFazal
 * before rendering its content); the layout itself is content-free
 * besides the banner so it doesn't need a separate auth gate.
 *
 * Banner is server-rendered with cached counts; client wrapper polls
 * `/api/ops/banner` every 30s. The API route gates on requireFazal,
 * so unauthenticated callers can't refresh + get fresh counts past the
 * initial render — the counts are aggregate (no per-tenant PII).
 */
export const dynamic = 'force-dynamic'

export default async function OpsLayout({ children }: { children: ReactNode }) {
  let counts: Awaited<ReturnType<typeof fetchBannerCounts>>
  try {
    counts = await fetchBannerCounts()
  } catch (err) {
    console.error('OpsLayout: banner fetch failed; rendering zero counts', err)
    counts = {
      escalations_24h: 0,
      aborted_hard_limits_24h: 0,
      errors_24h: 0,
      refreshed_at: new Date().toISOString(),
    }
  }

  return (
    <section data-area="team-ops">
      <StickyBannerLive initialCounts={counts} />
      {children}
    </section>
  )
}
