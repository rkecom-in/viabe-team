import type { ReactNode } from 'react'

import { OpsSideNav } from '@/components/ops/ops-side-nav'
import { OverlayProvider } from '@/components/ops/overlay-context'
import { StickyBannerLive } from '@/components/ops/sticky-banner-live'
import { OperatorRole } from '@/lib/auth/roles'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchBannerCounts } from '@/lib/ops/banner'

/**
 * Ops Console V2 shell (VT-290; extends the VT-201 banner shell).
 *
 * Single console: hoists the sticky banner + the role-gated side nav + the overlay
 * primitive (right-drawer; the "deep views are overlays, not detail pages" contract every
 * VT-291..298 sub-row inherits). Auth: resolves the operator's ROLE here for the nav; the
 * actual gate still lives per-page (each page calls requireOpsOperator/requireFazal +
 * redirects) so an unauthenticated visitor sees no nav and the page redirects to login.
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

  // Role for the nav (content-free fallback if unauthenticated — pages still gate + redirect).
  let role: OperatorRole | null = null
  try {
    role = (await requireOpsOperator()).role
  } catch {
    role = null
  }

  return (
    <section data-area="team-ops" className="min-h-screen bg-background text-foreground">
      <StickyBannerLive initialCounts={counts} />
      <OverlayProvider>
        <div data-ops-shell className="flex gap-0">
          {role && <OpsSideNav role={role} />}
          <div data-ops-main className="min-w-0 flex-1">
            {children}
          </div>
        </div>
      </OverlayProvider>
    </section>
  )
}
