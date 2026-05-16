import { NextResponse } from 'next/server'

/**
 * Twilio webhook receiver.
 *
 * Phase 1 scaffold: acknowledges receipt only. Signature verification
 * (TEAM_TWILIO_AUTH_TOKEN) and dispatch to the orchestrator land in a later
 * ticket.
 */
export async function POST() {
  return NextResponse.json({ received: true })
}
