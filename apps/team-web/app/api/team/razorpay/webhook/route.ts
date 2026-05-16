import { NextResponse } from 'next/server'

/**
 * Razorpay webhook receiver.
 *
 * Phase 1 scaffold: acknowledges receipt only. Signature verification
 * (TEAM_RAZORPAY_WEBHOOK_SECRET) and order handling land in a later ticket.
 */
export async function POST() {
  return NextResponse.json({ received: true })
}
