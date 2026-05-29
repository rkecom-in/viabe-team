/**
 * VT-201 sticky banner — surfaces last-24h escalations / hard-limits /
 * errors so operator doesn't have to hunt. Server-rendered with the
 * counts computed at request time.
 *
 * VT-235 styling pass — pill chips with spacing, amber baseline,
 * red intensification when errors_24h > 0.
 */

import type { BannerCounts } from '@/lib/ops/banner'

export interface StickyBannerProps {
  counts: BannerCounts
}

function severityClasses(counts: BannerCounts): {
  outer: string
  label: string
  value: string
  refresh: string
} {
  if (counts.errors_24h > 0) {
    return {
      outer: 'sticky top-0 z-10 bg-red-50 border-b border-red-200 px-4 py-2',
      label: 'text-xs uppercase tracking-wide text-red-700',
      value: 'font-semibold text-red-900',
      refresh: 'text-xs text-red-600',
    }
  }
  return {
    outer: 'sticky top-0 z-10 bg-amber-50 border-b border-amber-200 px-4 py-2',
    label: 'text-xs uppercase tracking-wide text-amber-700',
    value: 'font-semibold text-amber-900',
    refresh: 'text-xs text-amber-600',
  }
}

export function StickyBanner({ counts }: StickyBannerProps) {
  const c = severityClasses(counts)
  return (
    <aside
      className={`ops-stream-banner ${c.outer}`}
      data-component="sticky-banner"
      role="status"
    >
      <div className="flex flex-wrap gap-6 items-center text-sm">
        <span
          data-banner-counter="escalations_24h"
          className="flex gap-2 items-center"
        >
          <span className={c.label}>Escalations 24h</span>
          <span className={c.value}>{counts.escalations_24h}</span>
        </span>
        <span
          data-banner-counter="aborted_hard_limits_24h"
          className="flex gap-2 items-center"
        >
          <span className={c.label}>Hard-limits 24h</span>
          <span className={c.value}>{counts.aborted_hard_limits_24h}</span>
        </span>
        <span
          data-banner-counter="errors_24h"
          className="flex gap-2 items-center"
        >
          <span className={c.label}>Errors 24h</span>
          <span className={c.value}>{counts.errors_24h}</span>
        </span>
        <span data-banner-refresh className={`ml-auto ${c.refresh}`}>
          refreshed {new Date(counts.refreshed_at).toLocaleTimeString()}
        </span>
      </div>
    </aside>
  )
}
