import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { forwardSubscribe } from '@/lib/orchestrator-client'

/**
 * Razorpay subscription creation at trial→paid conversion (VT-331).
 *
 * Auth: requireFazal — the tenant is SERVER-DERIVED (FAZAL_TENANT_ID), NEVER from the
 * client body (IDOR-safe; Phase-1 single-tenant, matches onboard/answer). The body
 * carries only {plan_tier}. Forwards to the orchestrator — the money-authoritative
 * layer that resolves plan_tier -> plan_id/amount, makes the Razorpay vendor call
 * (STUB; LIVE NEEDS-FAZAL), and writes subscriptions. Does NOT flip phase — trial→paid
 * stays webhook-only (the VT-89 payment.captured path). 401 unauth; 400 bad body.
 *
 * (The multi-owner / JWT-deep-link conversion auth is VT-91.)
 */
export async function POST(request: NextRequest): Promise<Response> {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ ok: false, reason: 'unauthorized' }, { status: 401 })
    }
    throw err
  }

  const tenantId = process.env.FAZAL_TENANT_ID ?? ''
  if (!tenantId) {
    return NextResponse.json({ ok: false, reason: 'tenant_not_configured' }, { status: 503 })
  }

  let planTier = ''
  try {
    const body = (await request.json()) as { plan_tier?: string }
    planTier = body.plan_tier ?? ''
  } catch {
    return NextResponse.json({ ok: false, reason: 'malformed_body' }, { status: 400 })
  }
  if (!planTier) {
    return NextResponse.json({ ok: false, reason: 'plan_tier_required' }, { status: 400 })
  }

  // tenantId is server-derived — a client-supplied tenant_id in the body is ignored.
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
