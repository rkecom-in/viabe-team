/**
 * Phase-1 Fazal-only auth gate for the Ops Console (VT-123).
 *
 * Every `app/(app)/team/ops/**` route + every `/api/ops/**` API handler
 * calls `requireFazal()` first. The helper:
 *
 *   1. Reads the JWT from the `viabe_ops_jwt` cookie.
 *   2. Verifies signature + audience via `SUPABASE_JWT_SECRET`.
 *   3. Asserts `sub === FAZAL_OWNER_UUID`.
 *
 * On any failure it throws `UnauthorizedError`. Callers may catch +
 * redirect; API routes should respond 403.
 *
 * Per CL-22: Ops Console is Phase-1 launch-blocking; RBAC beyond
 * "Fazal-only" deferred to VT-189.
 *
 * Testability — accepts `getCookieJar` injection so unit tests can
 * supply mock cookies without invoking Next.js's `next/headers` (which
 * needs the framework runtime to populate).
 */

import { cookies } from 'next/headers'

import { verifyOperatorJwt } from './operator-jwt'

const FAZAL_UUID = process.env.FAZAL_OWNER_UUID ?? ''
const _COOKIE_NAME = 'viabe_ops_jwt'


export class UnauthorizedError extends Error {
  constructor(reason: string) {
    super(`requireFazal: ${reason}`)
    this.name = 'UnauthorizedError'
  }
}


interface CookieJar {
  get(name: string): { value: string } | undefined
}


/** Default cookie-jar — Next.js server runtime. */
async function _defaultCookieJar(): Promise<CookieJar> {
  return await cookies()
}


export async function requireFazal(
  getCookieJar: () => Promise<CookieJar> = _defaultCookieJar,
): Promise<{ fazalUuid: string }> {
  if (!FAZAL_UUID) {
    throw new UnauthorizedError('FAZAL_OWNER_UUID not configured on server')
  }
  const jar = await getCookieJar()
  const entry = jar.get(_COOKIE_NAME)
  if (!entry || !entry.value) {
    throw new UnauthorizedError('no operator JWT cookie')
  }
  let claim
  try {
    claim = await verifyOperatorJwt(entry.value)
  } catch (err) {
    throw new UnauthorizedError(`JWT verify failed: ${(err as Error).message}`)
  }
  if (claim.sub !== FAZAL_UUID) {
    throw new UnauthorizedError(
      `JWT subject mismatch — got ${claim.sub}, expected ${FAZAL_UUID}`,
    )
  }
  return { fazalUuid: claim.sub }
}
