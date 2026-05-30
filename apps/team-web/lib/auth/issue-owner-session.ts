/**
 * VT-250 — owner-session cookie helper.
 *
 * Mints the owner JWT (tenant-scoped, audience 'owner') + sets the
 * `viabe_team_session` cookie. DISTINCT from the operator cookie
 * (`viabe_ops_jwt`): different name, audience, signing secret, and a
 * hardened cookie posture per the VT-250 brief —
 *   HttpOnly + Secure + SameSite=Strict (the operator cookie is SameSite=lax).
 *
 * Caller responsibility: the caller MUST have verified the owner earned this
 * tenant identity — i.e. a Twilio Verify OTP check returned `approved` AND
 * the entered phone resolved to exactly this `tenantId` (D1 anchor). The
 * helper trusts the `tenantId` argument.
 *
 * Cowork D3: TTL ≤ 24h (the JWT exp + the cookie Max-Age both use
 * OWNER_SESSION_TTL_SEC).
 */

import { NextResponse } from 'next/server'

import { issueOwnerJwt, OWNER_SESSION_TTL_SEC } from './owner-jwt'

export const OWNER_COOKIE_NAME = 'viabe_team_session'
export const OWNER_COOKIE_TTL_SEC = OWNER_SESSION_TTL_SEC // ≤ 24h (D3)

/** The owner portal lives under /team — scope the cookie there. */
const OWNER_COOKIE_PATH = '/team'


/** Apply the owner-session cookie to an existing response. */
export function setOwnerSessionCookie(
  res: NextResponse,
  ownerJwt: string,
): NextResponse {
  res.cookies.set(OWNER_COOKIE_NAME, ownerJwt, {
    httpOnly: true,
    secure: true,
    sameSite: 'strict',
    path: OWNER_COOKIE_PATH,
    maxAge: OWNER_COOKIE_TTL_SEC,
  })
  return res
}


/** Mint a fresh owner JWT for `tenantId` and set it on `res`. */
export async function issueOwnerSession(
  tenantId: string,
  res: NextResponse,
): Promise<NextResponse> {
  const ownerJwt = await issueOwnerJwt(tenantId)
  return setOwnerSessionCookie(res, ownerJwt)
}


/** Clear the owner-session cookie (explicit logout). */
export function clearOwnerSessionCookie(res: NextResponse): NextResponse {
  res.cookies.set(OWNER_COOKIE_NAME, '', {
    httpOnly: true,
    secure: true,
    sameSite: 'strict',
    path: OWNER_COOKIE_PATH,
    maxAge: 0,
  })
  return res
}
