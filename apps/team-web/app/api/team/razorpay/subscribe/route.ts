import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { TrialEndTokenError, verifyTrialEndToken } from '@/lib/auth/verify-trial-end-token'
import { forwardSubscribe } from '@/lib/orchestrator-client'

/**
 * Resolve the subscribing tenant SERVER-SIDE from one of 3 auth paths, in order
 * (VT-91, Cowork Q1):
 *   1. portal owner session (`viabe_team_session` cookie) -> claim.tenant_id
 *   2. trial-end deep-link token (body.token) -> verified claim.tenant_id
 *   3. Fazal / Ops fallback (requireFazal) -> FAZAL_TENANT_ID
 *
 * The tenant ALWAYS comes from a verified claim (cookie JWT, token JWT, or the Fazal
 * env) — a raw client-supplied tenant_id is NEVER trusted (IDOR-safe on every path).
 * Throws an auth error if none authenticate. Returns '' only if Fazal authenticates but
 * FAZAL_TENANT_ID is unset (the caller maps that to 503).
 */
async function resolveSubscribeTenant(token: string): Promise<string> {
  // 1. Portal owner session (cookie). Absent/invalid -> fall through to the next path.
  try {
    const { tenantId } = await requireOwnerSession()
    return tenantId
  } catch (err) {
    if (!(err instanceof OwnerUnauthorizedError)) throw err
  }
  // 2. Trial-end deep-link token (body.token). A bad/expired/wrong-audience token throws.
  if (token) {
    const { tenantId } = await verifyTrialEndToken(token)
    return tenantId
  }
  // 3. Fazal / Ops fallback. Throws UnauthorizedError if not Fazal.
  await requireFazal()
  return process.env.FAZAL_TENANT_ID ?? ''
}

/**
 * Razorpay subscription creation at trial→paid conversion (VT-331 backend, VT-91
 * frontend auth). Body = `{plan_tier, token?}`. Auth via resolveSubscribeTenant (above).
 * Forwards to the orchestrator (money-authoritative — resolves plan, vendor call, writes
 * subscriptions). Does NOT flip phase — conversion stays webhook-only (VT-89). 401 unauth;
 * 400 bad body; 503 tenant not configured; 502 orchestrator failure.
 */
export async function POST(request: NextRequest): Promise<Response> {
  let planTier = ''
  let token = ''
  try {
    const body = (await request.json()) as { plan_tier?: string; token?: string }
    planTier = body.plan_tier ?? ''
    token = body.token ?? ''
  } catch {
    return NextResponse.json({ ok: false, reason: 'malformed_body' }, { status: 400 })
  }
  if (!planTier) {
    return NextResponse.json({ ok: false, reason: 'plan_tier_required' }, { status: 400 })
  }

  let tenantId: string
  try {
    tenantId = await resolveSubscribeTenant(token)
  } catch (err) {
    if (
      err instanceof OwnerUnauthorizedError ||
      err instanceof TrialEndTokenError ||
      err instanceof UnauthorizedError
    ) {
      return NextResponse.json({ ok: false, reason: 'unauthorized' }, { status: 401 })
    }
    throw err
  }
  if (!tenantId) {
    return NextResponse.json({ ok: false, reason: 'tenant_not_configured' }, { status: 503 })
  }

  // tenantId is server-derived from a verified claim — a client tenant_id is never used.
  const result = await forwardSubscribe(tenantId, planTier)
  if (!result.ok) {
    return NextResponse.json({ ok: false, reason: result.status }, { status: 502 })
  }
  return NextResponse.json({
    ok: true,
    status: result.status,
    razorpaySubscriptionId: result.razorpaySubscriptionId,
  })
}
