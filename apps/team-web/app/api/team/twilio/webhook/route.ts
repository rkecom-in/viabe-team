import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

import { forwardToOrchestrator } from '@/lib/orchestrator-client'
import { parseTwilioBody, verifyTwilioSignature } from '@/lib/twilio'

// Empty TwiML — acknowledges the webhook without sending a reply.
const TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response/>'

function twimlOk(): NextResponse {
  return new NextResponse(TWIML_EMPTY, {
    status: 200,
    headers: { 'content-type': 'text/xml' },
  })
}

/**
 * Twilio inbound WhatsApp webhook (VT-3.3b).
 *
 * Deterministic ingress only (Pillar 1): verify the Twilio signature, then
 * forward the raw fields to the orchestrator. Tenant lookup and rate limiting
 * live in the orchestrator (Pillar 8 — a single DB-access path).
 *
 * Pillar 7: never returns 5xx for an application error — a 5xx would trigger a
 * Twilio retry and duplicate processing. 403 only for a bad signature.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const rawBody = await request.text()
  const signature = request.headers.get('x-twilio-signature')
  const webhookUrl = process.env.TEAM_TWILIO_WEBHOOK_URL ?? request.url
  const params = parseTwilioBody(rawBody)

  if (!verifyTwilioSignature(signature, webhookUrl, params)) {
    return new NextResponse('forbidden', { status: 403 })
  }

  const twilioFields: Record<string, string> = {
    From: params.From ?? '',
    To: params.To ?? '',
    Body: params.Body ?? '',
    MessageSid: params.MessageSid ?? '',
    NumMedia: params.NumMedia ?? '0',
    MediaUrl0: params.MediaUrl0 ?? '',
    MessageStatus: params.MessageStatus ?? '',
  }

  const result = await forwardToOrchestrator(twilioFields)
  if (!result.ok) {
    // Pillar 7: still 200 to Twilio. VT-3.6's retry framework consumes this log.
    console.error(
      JSON.stringify({
        event: 'orchestrator_forward_failed',
        reason: result.reason,
        message_sid: twilioFields.MessageSid,
      }),
    )
  }
  return twimlOk()
}
