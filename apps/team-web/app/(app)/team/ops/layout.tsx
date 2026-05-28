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
  // The banner is only useful to authed operators; render-time auth
  // mismatch is rare in Phase-1 (single Fazal). Skip the banner on
  // auth failure rather than redirect — child pages handle their own
  // requireFazal + redirect. Likewise tolerate transient Supabase
  // failures (CI stub, dev offline) — the page-level data fetches
  // still surface their own errors; the banner is not load-bearing.
  let counts = null
  try {
    await requireFazal()
    counts = await fetchBannerCounts()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      counts = null
    } else {
      console.error('OpsLayout: banner fetch failed; skipping banner', err)
      counts = null
    }
  }

  return (
    <section data-area="team-ops">
      {counts && <StickyBannerLive initialCounts={counts} />}
      {children}
    </section>
  )
}
