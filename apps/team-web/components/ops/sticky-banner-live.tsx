'use client'

/**
 * VT-201 PR-3 — client wrapper around StickyBanner with 30s auto-refresh.
 *
 * Q3 lock: severity = max(escalations, hard_limits, errors). 0 → green,
 * 1-2 → yellow, 3+ → red. Encoded as `data-severity` attribute on the
 * wrapper; CSS handles the actual colour.
 *
 * Server passes initial counts; client polls /api/ops/banner every 30s.
 * Server cache is 60s (Cowork lock: don't double-tighten).
 */

import { useEffect, useState } from 'react'

import { StickyBanner } from '@/components/ops/sticky-banner'
import type { BannerCounts } from '@/lib/ops/banner'

const _REFRESH_INTERVAL_MS = 30_000

function severity(counts: BannerCounts): 'green' | 'yellow' | 'red' {
  const max = Math.max(
    counts.escalations_24h,
    counts.aborted_hard_limits_24h,
    counts.errors_24h,
  )
  if (max === 0) return 'green'
  if (max <= 2) return 'yellow'
  return 'red'
}

export interface StickyBannerLiveProps {
  initialCounts: BannerCounts
}

export function StickyBannerLive({ initialCounts }: StickyBannerLiveProps) {
  const [counts, setCounts] = useState<BannerCounts>(initialCounts)

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const res = await fetch('/api/ops/banner', { cache: 'no-store' })
        if (!res.ok) return
        const data = (await res.json()) as BannerCounts
        if (!cancelled) {
          setCounts(data)
        }
      } catch {
        // Best-effort poll; transient fetch errors are non-fatal.
      }
    }
    const id = window.setInterval(tick, _REFRESH_INTERVAL_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [])

  return (
    <div
      data-component="sticky-banner-live"
      data-severity={severity(counts)}
    >
      <StickyBanner counts={counts} />
    </div>
  )
}
