/**
 * Supabase JS clients for the Ops Console (VT-123).
 *
 * Per VT-2 key discipline: only the new publishable + secret key model
 * is supported. Legacy Supabase anon / service-role key naming is
 * BANNED at CI (scripts/lint-cross-product-env.mjs).
 *
 * Two factories:
 *
 * - `serverSecretClient()` — server-only; uses `TEAM_SUPABASE_SECRET_KEY`.
 *   Bypasses RLS (the new "secret" key is the publishable/secret-pair
 *   replacement for the legacy service-role key). Use for cold-read
 *   aggregations on Ops UI pages (per-tenant timeline, run replay,
 *   privacy audit log readout).
 *
 * - `serverOperatorClient(jwt)` — server-only; uses publishable key + a
 *   caller-supplied operator-claim JWT. RLS-enforced via the JWT's
 *   `tenant_id` claim (set by `lib/auth/operator-jwt.ts`). Use for the
 *   [resolve] route's `app_operator_role` RPC path (VT-188 substrate).
 *
 * Per CL-52: cold-read aggregations use the secret-key pool directly.
 * Per CL-88: hot reads + write paths use JWT-direct with RLS.
 */

import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const SUPABASE_URL = process.env.TEAM_SUPABASE_URL ?? ''
const SECRET_KEY = process.env.TEAM_SUPABASE_SECRET_KEY ?? ''
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_TEAM_SUPABASE_PUBLISHABLE_KEY ?? ''


export function serverSecretClient(): SupabaseClient {
  if (!SUPABASE_URL || !SECRET_KEY) {
    throw new Error(
      'serverSecretClient: TEAM_SUPABASE_URL and TEAM_SUPABASE_SECRET_KEY ' +
        'must be set in the server env',
    )
  }
  return createClient(SUPABASE_URL, SECRET_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  })
}


export function serverOperatorClient(jwt: string): SupabaseClient {
  if (!SUPABASE_URL || !PUBLISHABLE_KEY) {
    throw new Error(
      'serverOperatorClient: TEAM_SUPABASE_URL and ' +
        'NEXT_PUBLIC_TEAM_SUPABASE_PUBLISHABLE_KEY must be set in the server env',
    )
  }
  return createClient(SUPABASE_URL, PUBLISHABLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: {
      headers: {
        Authorization: `Bearer ${jwt}`,
      },
    },
  })
}
