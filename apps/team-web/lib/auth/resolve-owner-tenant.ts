/**
 * VT-250 — owner_phone → tenant resolution (the D1 anchor lookup).
 *
 * Maps a normalized E.164 owner phone to exactly one tenant via the globally
 * unique `tenants.owner_phone` index (migration 050). Uses the server secret
 * client (server-only; bypasses RLS) because the login surface is PRE-session
 * — there is no tenant scope yet to enforce RLS against; the unique index is
 * the integrity guarantee that exactly one tenant matches.
 *
 * Returns the tenant_id, or null when no tenant owns that phone (login fails
 * closed — an unknown phone never mints a session). The phone is passed in
 * already-normalized; this module does no normalization (single normalization
 * point lives in `owner-phone.ts`).
 *
 * CL-390: never logs the phone. A miss returns null; the caller emits only a
 * generic outcome.
 */

import { serverSecretClient } from '@/lib/supabase-client'

type DbClient = { from: (table: string) => any }

export async function resolveOwnerTenant(
  phoneE164: string,
  client?: DbClient,
): Promise<string | null> {
  const c = client ?? serverSecretClient()
  const { data, error } = await c
    .from('tenants')
    .select('id')
    .eq('owner_phone', phoneE164)
    .maybeSingle()
  if (error || !data) {
    return null
  }
  return (data as { id: string }).id
}
