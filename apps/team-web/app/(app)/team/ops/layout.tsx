import type { ReactNode } from 'react'

import { StickyBannerLive } from '@/components/ops/sticky-banner-live'
import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { fetchBannerCounts } from '@/lib/ops/banner'

/**
 * Ops UI shell — Fazal-only.
 *
 * VT-201 PR-3: hoists the sticky banner here so all `/team/ops/**` pages
 * render the last-24h operator-awareness counts without per-page wiring.
 * The wrapper is a client component; counts auto-refresh every 30s.
 */
export const dynamic = 'force-dynamic'

export default async function OpsLayout({ children }: { children: ReactNode }) {
  // The banner is only useful to authed operators; on auth failure
  // hide it entirely. On data-fetch failure (Supabase stub, transient
  // outage) render the banner with zero counts — the client poll
  // recovers as soon as the backend is reachable.
  let counts: Awaited<ReturnType<typeof fetchBannerCounts>> | null = null
  let isAuthed = false
  try {
    await requireFazal()
    isAuthed = true
  } catch (err) {
    if (!(err instanceof UnauthorizedError)) throw err
  }
  if (isAuthed) {
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
  }

  return (
    <section data-area="team-ops">
      {counts && <StickyBannerLive initialCounts={counts} />}
      {children}
    </section>
  )
}
