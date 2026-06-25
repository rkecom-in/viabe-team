import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

import { forwardRazorpayEvent } from '@/lib/orchestrator-client'
import { verifyRazorpaySignature } from '@/lib/razorpay'

/**
 * Razorpay webhook receiver (VT-89).
 *
 * Verify the HMAC (RAZORPAY_WEBHOOK_SECRET), then
 * forward the event to the orchestrator's razorpay-ingress — the durable inbox +
 * the sole writer of subscription fee state + billing phase transitions.
 *
 * Q1 (financial durability): return 200 to Razorpay ONLY after the orchestrator
 * confirms the event was DURABLY persisted (its 2xx). If the orchestrator is
 * unreachable or errors before persisting, return 5xx so Razorpay RETRIES — a lost
 * `subscription.charged` would silently undercount fees and under-refund later.
 * Bad signature -> 403. Malformed body -> 400.
 *
 * LIVE keys are NEEDS-FAZAL, hard-gated by VT-93-N1 + VT-329.
 */
export async function POST(req: NextRequest): Promise<NextResponse> {
  // Raw body (not re-serialised JSON) — the HMAC is computed over the exact bytes.
  const rawBody = await req.text()
  const signature = req.headers.get('x-razorpay-signature')
  const secret = process.env.RAZORPAY_WEBHOOK_SECRET ?? ''

  if (!verifyRazorpaySignature(signature, rawBody, secret)) {
    return NextResponse.json({ error: 'invalid signature' }, { status: 403 })
  }

  let event: { id?: string; event?: string; payload?: unknown }
  try {
    event = JSON.parse(rawBody)
  } catch {
    return NextResponse.json({ error: 'malformed body' }, { status: 400 })
  }
  if (!event.id || !event.event) {
    return NextResponse.json({ error: 'missing event id/type' }, { status: 400 })
  }

  const result = await forwardRazorpayEvent(
    event.id,
    event.event,
    (event.payload as Record<string, unknown>) ?? {},
  )
  if (!result.ok) {
    // Q1: NOT durably recorded -> 5xx so Razorpay retries (never silently drop a
    // financial event). 502: the orchestrator (upstream) is unavailable/errored.
    return NextResponse.json(
      { error: 'ingress unavailable', reason: result.status },
      { status: 502 },
    )
  }
  return NextResponse.json({ received: true, status: result.status })
}
