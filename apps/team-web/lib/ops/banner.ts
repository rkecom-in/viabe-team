/**
 * VT-201 sticky-banner aggregate — server-side cached at ~60s refresh.
 *
 * Per Cowork Q3 Option locked at plan-review: server-side cached
 * banner counts (escalations / hard-limits / errors in last 24h)
 * reduces DB load vs continuous subscription. Cache key includes the
 * time-window so different filter selections don't collide.
 *
 * Per CL-52: cold-read aggregations use the secret-key server client.
 */

import { serverSecretClient } from '@/lib/supabase-client'

export interface BannerCounts {
  escalations_24h: number
  aborted_hard_limits_24h: number
  errors_24h: number
  refreshed_at: string
}

interface CacheEntry {
  value: BannerCounts
  expires_at: number
}

const _BANNER_TTL_MS = 60_000  // 60s per Cowork Q3 Option locked
const _BANNER_CACHE = new Map<string, CacheEntry>()


export async function fetchBannerCounts(): Promise<BannerCounts> {
  const cacheKey = 'last_24h'
  const now = Date.now()
  const cached = _BANNER_CACHE.get(cacheKey)
  if (cached && cached.expires_at > now) {
    return cached.value
  }

  const client = serverSecretClient()
  const since = new Date()
  since.setUTCHours(since.getUTCHours() - 24)
  const sinceIso = since.toISOString()

  const [escalations, hardLimits, errors] = await Promise.all([
    client
      .from('pipeline_runs')
      .select('id', { count: 'exact', head: true })
      .eq('status', 'escalated')
      .gte('started_at', sinceIso),
    client
      .from('pipeline_runs')
      .select('id', { count: 'exact', head: true })
      .eq('status', 'aborted_hard_limit')
      .gte('started_at', sinceIso),
    client
      .from('pipeline_steps')
      .select('id', { count: 'exact', head: true })
      .eq('status', 'failed')
      .gte('started_at', sinceIso),
  ])

  const value: BannerCounts = {
    escalations_24h: escalations.count ?? 0,
    aborted_hard_limits_24h: hardLimits.count ?? 0,
    errors_24h: errors.count ?? 0,
    refreshed_at: new Date().toISOString(),
  }
  _BANNER_CACHE.set(cacheKey, { value, expires_at: now + _BANNER_TTL_MS })
  return value
}
