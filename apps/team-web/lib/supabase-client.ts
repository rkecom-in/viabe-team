/**
 * Supabase JS clients for the Ops Console (VT-123).
 *
 * Per Fazal directive 2026-05-27: the Supabase project uses the NEW
 * publishable + secret key system. Legacy anon / service-role naming is
 * not used here (and is BANNED at CI via scripts/lint-cross-product-env.mjs).
 *
 * Two factories:
 *
 * - `serverSecretClient()` — server-only; uses `SUPABASE_SECRET_KEY`.
 *   Bypasses RLS (server-side counterpart of the publishable/secret pair;
 *   functionally equivalent to the legacy service-role key for cold-read
 *   aggregations on Ops UI pages — per-tenant timeline, run replay,
 *   privacy audit log readout).
 *
 * - `serverOperatorClient(jwt)` — server-only; uses publishable key + a
 *   caller-supplied operator-claim JWT. RLS-enforced via the JWT's
 *   `tenant_id` claim (set by `lib/auth/operator-jwt.ts`). Use for the
 *   [resolve] route's `app_operator_role` RPC path (VT-188 substrate).
 *
 * Per CL-52: cold-read aggregations use the secret-key client.
 * Per CL-88: hot reads + write paths use JWT-direct with RLS.
 */

import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const SUPABASE_URL = process.env.SUPABASE_URL ?? ''
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY ?? ''
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? ''


export function serverSecretClient(): SupabaseClient {
  if (!SUPABASE_URL || !SECRET_KEY) {
    throw new Error(
      'serverSecretClient: SUPABASE_URL and SUPABASE_SECRET_KEY ' +
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
      'serverOperatorClient: SUPABASE_URL and ' +
        'NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY must be set in the server env',
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
