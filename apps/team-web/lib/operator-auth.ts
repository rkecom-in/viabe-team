/**
 * VT-192 — operator JWT validation wrapper for team-web routes.
 *
 * `requireOperator(req)` extracts the operator JWT from either the
 * Authorization header (Bearer) or the `viabe_ops_jwt` cookie (VT-203
 * sets this), verifies the HS256 signature against `OPERATOR_JWT_SECRET`,
 * and returns the decoded operator claim. Throws `UnauthorizedError` on
 * any failure mode (missing token, invalid sig, malformed claim).
 *
 * Reused by VT-203's Ops Console login route + VT-192's resolve-phone
 * proxy route + every future operator-only API route.
 */

import { verifyOperatorJwt, type OperatorClaim } from '@/lib/auth/operator-jwt'

export class UnauthorizedError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'UnauthorizedError'
  }
}

function extractToken(req: Request): string | null {
  const auth = req.headers.get('authorization')
  if (auth && auth.toLowerCase().startsWith('bearer ')) {
    return auth.slice(7).trim()
  }
  // Fallback to cookie (VT-203 sets viabe_ops_jwt). Parse manually since
  // we don't have cookies() helper in route-handler scope without Next
  // imports.
  const cookieHeader = req.headers.get('cookie') ?? ''
  for (const part of cookieHeader.split(';')) {
    const [name, ...rest] = part.trim().split('=')
    if (name === 'viabe_ops_jwt') {
      return rest.join('=').trim() || null
    }
  }
  return null
}

export interface RequireOperatorResult {
  operatorId: string
  claim: OperatorClaim
  rawToken: string
}

export async function requireOperator(req: Request): Promise<RequireOperatorResult> {
  const rawToken = extractToken(req)
  if (!rawToken) {
    throw new UnauthorizedError('no operator JWT (Authorization Bearer or viabe_ops_jwt cookie required)')
  }
  let claim: OperatorClaim
  try {
    claim = await verifyOperatorJwt(rawToken)
  } catch (err) {
    throw new UnauthorizedError(
      `operator JWT invalid: ${err instanceof Error ? err.message : 'unknown'}`,
    )
  }
  return {
    operatorId: claim.operator_id,
    claim,
    rawToken,
  }
}
