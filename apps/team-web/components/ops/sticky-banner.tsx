/**
 * VT-201 sticky banner — surfaces last-24h escalations / hard-limits /
 * errors so operator doesn't have to hunt. Server-rendered with the
 * counts computed at request time.
 *
 * VT-235 styling pass — pill chips with spacing, amber baseline,
 * red intensification when errors_24h > 0.
 *
 * VT-380 (B3): toLocaleTimeString() is locale- and timezone-dependent —
 * the server may produce "14:32:01" while the client hydrates with
 * "2:32:01 PM" (or vice-versa) → React minified error #418 on EVERY
 * /team/ops/* page. Replaced with a deterministic UTC HH:MM:SS formatter
 * derived from the ISO timestamp value so SSR and client always match.
 */

import type { BannerCounts } from '@/lib/ops/banner'

/**
 * Format an ISO timestamp as a zero-padded UTC HH:MM:SS string.
 * Deterministic across every JS environment — no locale, no timezone
 * variance. E.g. "2026-06-12T14:32:01.000Z" → "14:32:01 UTC".
 */
function utcTimeString(isoTimestamp: string): string {
  const d = new Date(isoTimestamp)
  if (isNaN(d.getTime())) return isoTimestamp // passthrough on invalid input
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  const ss = String(d.getUTCSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss} UTC`
}

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
          refreshed {utcTimeString(counts.refreshed_at)}
        </span>
      </div>
    </aside>
  )
}
