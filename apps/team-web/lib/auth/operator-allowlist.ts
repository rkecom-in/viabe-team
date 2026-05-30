/**
 * VT-228 — operator allowlist check (replaces the hardcoded
 * FAZAL_OWNER_UUID comparison at the VT-203/233/237 auth callsites).
 *
 * `isOperator(userId)` is the single source of truth:
 *   1. Break-glass: `userId === FAZAL_OWNER_UUID` is ALWAYS an operator
 *      (no lock-out if the table is empty/unreachable; scoped to that one
 *      UUID). Must stay until the env check is fully retired.
 *   2. Otherwise: a non-revoked row in `operator_allowlist`.
 *
 * Per-request callers (require-fazal) hit this on every Ops request, so
 * the DB result is cached in-process for 30s — bounds cost while keeping
 * revoke near-immediate (≤30s), far better than the 7-day JWT lifetime.
 *
 * The Supabase client is injectable for tests (default = serverSecretClient,
 * which bypasses RLS — the deny-all table is service-role only).
 */

import { serverSecretClient } from '@/lib/supabase-client'

const FAZAL_UUID = (process.env.FAZAL_OWNER_UUID ?? '').trim()
const _CACHE_TTL_MS = 30_000

type AllowlistClient = {
  from: (table: string) => any
}

// userId -> { allowed, expires }
const _cache = new Map<string, { allowed: boolean; expires: number }>()

/** Test seam: clear the in-process cache. */
export function _clearOperatorCache(): void {
  _cache.clear()
}

export async function isOperator(
  userId: string,
  client?: AllowlistClient,
  now: number = Date.now(),
): Promise<boolean> {
  if (!userId) return false
  // 1. Break-glass — Fazal's UUID always passes (no DB client constructed,
  //    no lock-out). Critical: do NOT build the Supabase client before
  //    this check (env may be absent on the env-password path).
  if (FAZAL_UUID && userId === FAZAL_UUID) return true

  // 2. Cache.
  const hit = _cache.get(userId)
  if (hit && hit.expires > now) return hit.allowed

  // 3. Table lookup — non-revoked row? (Lazily construct the client here.)
  let allowed = false
  try {
    const c = client ?? serverSecretClient()
    const { data, error } = await c
      .from('operator_allowlist')
      .select('user_id')
      .eq('user_id', userId)
      .is('revoked_at', null)
      .maybeSingle()
    if (error) {
      // Table missing / DB unreachable → fail CLOSED for non-Fazal
      // (Fazal already returned true above via break-glass).
      allowed = false
    } else {
      allowed = data != null
    }
  } catch {
    allowed = false
  }

  _cache.set(userId, { allowed, expires: now + _CACHE_TTL_MS })
  return allowed
}
