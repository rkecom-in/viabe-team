/**
 * VT-201 sticky banner — surfaces last-24h escalations / hard-limits /
 * errors so operator doesn't have to hunt. Server-rendered with the
 * counts computed at request time (`fetchBannerCounts` is server-side
 * cached at 60s TTL per Cowork Q3 lock).
 */

import type { BannerCounts } from '@/lib/ops/banner'

export interface StickyBannerProps {
  counts: BannerCounts
}

export function StickyBanner({ counts }: StickyBannerProps) {
  return (
    <aside
      className="ops-stream-banner"
      data-component="sticky-banner"
      role="status"
    >
      <span data-banner-counter="escalations_24h">
        Escalations 24h: {counts.escalations_24h}
      </span>
      <span data-banner-counter="aborted_hard_limits_24h">
        Hard-limits 24h: {counts.aborted_hard_limits_24h}
      </span>
      <span data-banner-counter="errors_24h">
        Errors 24h: {counts.errors_24h}
      </span>
      <span data-banner-refresh>
        refreshed {new Date(counts.refreshed_at).toLocaleTimeString()}
      </span>
    </aside>
  )
}
