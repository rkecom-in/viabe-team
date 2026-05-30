/**
 * VT-250 — owner-portal auth gate (the OWNER analog of `require-fazal.ts`).
 *
 * Every owner-facing portal route + owner API handler calls
 * `requireOwnerSession()` first. The helper:
 *
 *   1. Reads the JWT from the `viabe_team_session` cookie.
 *   2. Verifies signature + audience ('owner') via `OWNER_JWT_SECRET`.
 *   3. Returns the tenant_id so the caller can scope RLS to that tenant.
 *
 * On any failure it throws `OwnerUnauthorizedError`. Page callers may catch +
 * redirect to /team/login; API routes should respond 401/403.
 *
 * NO-PRIVILEGE-CROSSOVER (VT-250 hard requirement):
 *   - This gate rejects an OPERATOR token: the operator token's audience is
 *     'authenticated' (not 'owner') and it is signed with OPERATOR_JWT_SECRET
 *     (not OWNER_JWT_SECRET), so `verifyOwnerJwt` fails on both counts.
 *   - Conversely `require-fazal` / `verifyOperatorJwt` rejects an owner token
 *     (audience 'authenticated' mismatch + secret mismatch).
 *   Neither token can be replayed as the other. Both directions are tested.
 *
 * Testability — accepts `getCookieJar` injection so unit tests can supply
 * mock cookies without invoking Next.js's `next/headers`.
 */

import { cookies } from 'next/headers'

import { OWNER_COOKIE_NAME } from './issue-owner-session'
import { verifyOwnerJwt } from './owner-jwt'


export class OwnerUnauthorizedError extends Error {
  constructor(reason: string) {
    super(`requireOwnerSession: ${reason}`)
    this.name = 'OwnerUnauthorizedError'
  }
}


interface CookieJar {
  get(name: string): { value: string } | undefined
}


/** Default cookie-jar — Next.js server runtime. */
async function _defaultCookieJar(): Promise<CookieJar> {
  return await cookies()
}


export async function requireOwnerSession(
  getCookieJar: () => Promise<CookieJar> = _defaultCookieJar,
): Promise<{ tenantId: string }> {
  const jar = await getCookieJar()
  const entry = jar.get(OWNER_COOKIE_NAME)
  if (!entry || !entry.value) {
    throw new OwnerUnauthorizedError('no owner-session cookie')
  }
  let claim
  try {
    claim = await verifyOwnerJwt(entry.value)
  } catch (err) {
    throw new OwnerUnauthorizedError(
      `owner JWT verify failed: ${(err as Error).message}`,
    )
  }
  return { tenantId: claim.tenant_id }
}
