import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { TrialEndTokenError, verifyTrialEndToken } from '@/lib/auth/verify-trial-end-token'
import { forwardSubscribe } from '@/lib/orchestrator-client'

/**
 * Resolve the subscribing tenant SERVER-SIDE from one of 2 auth paths, in order
 * (VT-91, Cowork Q1; VT-416 PR-1 dropped the path-3 FAZAL_TENANT_ID fallback):
 *   1. portal owner session (`viabe_team_session` cookie) -> claim.tenant_id
 *   2. trial-end deep-link token (body.token) -> verified claim.tenant_id
 *
 * The tenant ALWAYS comes from a verified claim (cookie JWT or token JWT) — a raw
 * client-supplied tenant_id is NEVER trusted (IDOR-safe on every path). There is NO
 * env-tenant fallback: a real owner ALWAYS resolves at path 1 or 2, so a fallback could
 * only ever cross-attribute a subscription to the wrong (Fazal's) tenant under a future
 * auth-order regression — a billing P0 we close by removing it (VT-416). A caller that
 * authenticates on no path returns '' (the caller maps that to 401 unauthorized —
 * fail-closed: it NEVER silently bills another tenant).
 */
async function resolveSubscribeTenant(
  token: string,
): Promise<{ tenantId: string; jti: string | null }> {
  // 1. Portal owner session (cookie). Absent/invalid -> fall through to the next path.
  try {
    const { tenantId } = await requireOwnerSession()
    return { tenantId, jti: null } // in-app path — no single-use token
  } catch (err) {
    if (!(err instanceof OwnerUnauthorizedError)) throw err
  }
  // 2. Trial-end deep-link token (body.token). A bad/expired/wrong-audience token throws.
  // VT-332: carry the token's jti through so the orchestrator can consume it single-use.
  if (token) {
    return await verifyTrialEndToken(token)
  }
  // No auth path matched (no session, no token). This caller is genuinely
  // unauthenticated — the case that used to hit the deleted path-3 FAZAL_TENANT_ID
  // fallback. Fail closed with an empty tenant; the caller maps '' to 401 unauthorized
  // (NOT 503 — the caller is unauthenticated, the service is not down). There is
  // deliberately NO Fazal/env fallback here (VT-416 PR-1): a real owner always resolves
  // above, so any fallback is pure cross-attribution risk.
  return { tenantId: '', jti: null }
}

/**
 * Razorpay subscription creation at trial→paid conversion (VT-331 backend, VT-91
 * frontend auth). Body = `{plan_tier, token?}`. Auth via resolveSubscribeTenant (above).
 * Forwards to the orchestrator (money-authoritative — resolves plan, vendor call, writes
 * subscriptions). Does NOT flip phase — conversion stays webhook-only (VT-89). 401 unauth
 * (no session AND no valid token — incl. the no-auth-path fall-through, Cowork gate ruling
 * VT-416 PR-1); 400 bad body; 502 orchestrator failure.
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

  let tenantId = ''
  let jti: string | null = null
  try {
    ;({ tenantId, jti } = await resolveSubscribeTenant(token))
  } catch (err) {
    if (err instanceof OwnerUnauthorizedError || err instanceof TrialEndTokenError) {
      return NextResponse.json({ ok: false, reason: 'unauthorized' }, { status: 401 })
    }
    throw err
  }
  if (!tenantId) {
    // The only path that yields an empty tenant is the no-session-AND-no-token
    // fall-through in resolveSubscribeTenant — a genuinely unauthenticated caller. 401 is
    // the accurate semantic (Cowork gate ruling, VT-416 PR-1); 503 would false-signal
    // "service DOWN" to monitoring when nothing is down.
    return NextResponse.json({ ok: false, reason: 'unauthorized' }, { status: 401 })
  }

  // tenantId is server-derived from a verified claim — a client tenant_id is never used. jti
  // (when present) flows to the orchestrator for the single-use consume (VT-332).
  const result = await forwardSubscribe(tenantId, planTier, jti)
  if (!result.ok) {
    return NextResponse.json({ ok: false, reason: result.status }, { status: 502 })
  }
  return NextResponse.json({
    ok: true,
    status: result.status,
    razorpaySubscriptionId: result.razorpaySubscriptionId,
  })
}
