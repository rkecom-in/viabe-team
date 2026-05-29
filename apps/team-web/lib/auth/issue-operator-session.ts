/**
 * VT-237 — shared operator-session helper.
 *
 * Mints the operator JWT (VT-203/VT-233 substrate) + sets the
 * `viabe_ops_jwt` cookie with the VT-230 path=/team + VT-236 7-day
 * Max-Age. Used by the magic-link callback, the implicit-flow
 * finalize-hash POST, and the new env-password login path.
 *
 * Caller responsibility: allowlist check. The helper trusts the
 * `operatorId` argument — pass FAZAL_OWNER_UUID only after verifying
 * the caller earned that identity (magic-link verified, hash verified,
 * or password matched).
 */

import { NextResponse } from 'next/server'

import { issueOperatorJwt } from './operator-jwt'
import { safeNext } from './safe-next'

export const OPERATOR_COOKIE_NAME = 'viabe_ops_jwt'
export const OPERATOR_COOKIE_TTL_SEC = 60 * 60 * 24 * 7 // 7 days (VT-236)

export interface IssueOperatorSessionInput {
  operatorId: string
  rawNext: string | null | undefined
  requestUrl: string
}

export async function issueOperatorSessionRedirect(
  input: IssueOperatorSessionInput,
): Promise<NextResponse> {
  const next = safeNext(input.rawNext ?? null)
  const opJwt = await issueOperatorJwt(input.operatorId)
  const res = NextResponse.redirect(new URL(next, input.requestUrl), {
    status: 302,
  })
  res.cookies.set(OPERATOR_COOKIE_NAME, opJwt, {
    httpOnly: true,
    secure: true,
    sameSite: 'lax',
    path: '/team',
    maxAge: OPERATOR_COOKIE_TTL_SEC,
  })
  return res
}
