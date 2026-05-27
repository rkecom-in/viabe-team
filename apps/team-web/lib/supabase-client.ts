/**
 * Supabase JS clients for the Ops Console (VT-123).
 *
 * Two factories:
 *
 * - `serverServiceRoleClient()` — server-only; uses SUPABASE_SERVICE_ROLE_KEY.
 *   Bypasses RLS. Use for cold-read aggregations on Ops UI pages
 *   (per-tenant timeline, run replay, privacy audit log readout).
 *
 * - `serverOperatorClient(jwt)` — server-only; uses anon key + a caller-
 *   supplied operator-claim JWT. RLS-enforced via the JWT's `tenant_id`
 *   claim (set by `lib/auth/operator-jwt.ts`). Use for the [resolve]
 *   route's `app_operator_role` RPC path (VT-188 substrate).
 *
 * Per CL-52: cold-read aggregations use the service-role pool directly.
 * Per CL-88: hot reads + write paths use JWT-direct with RLS.
 */

import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const SUPABASE_URL = process.env.SUPABASE_URL ?? ''
const SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY ?? ''
const ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? ''


export function serverServiceRoleClient(): SupabaseClient {
  if (!SUPABASE_URL || !SERVICE_ROLE_KEY) {
    throw new Error(
      'serverServiceRoleClient: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY ' +
        'must be set in the server env',
    )
  }
  return createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  })
}


export function serverOperatorClient(jwt: string): SupabaseClient {
  if (!SUPABASE_URL || !ANON_KEY) {
    throw new Error(
      'serverOperatorClient: SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY ' +
        'must be set in the server env',
    )
  }
  return createClient(SUPABASE_URL, ANON_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: {
      headers: {
        Authorization: `Bearer ${jwt}`,
      },
    },
  })
}
