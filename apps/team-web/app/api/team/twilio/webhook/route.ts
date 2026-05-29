import { createHash } from 'crypto'

import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

import { redactForLog } from '@/lib/log-redact'
import { forwardToOrchestrator } from '@/lib/orchestrator-client'
import { serverSecretClient } from '@/lib/supabase-client'
import { parseTwilioBody, verifyTwilioSignature } from '@/lib/twilio'

// Empty TwiML — acknowledges the webhook without sending a reply.
const TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response/>'

function twimlOk(): NextResponse {
  return new NextResponse(TWIML_EMPTY, {
    status: 200,
    headers: { 'content-type': 'text/xml' },
  })
}

// VT-81 in-memory sliding-window rate limit per source IP.
// 30 req/min — Twilio's sustained inbound rate per number is well
// under this; bursts above suggest abuse. Cold-start gaps acceptable.
const _RATE_WINDOW_MS = 60_000
const _RATE_LIMIT = 30
const _rateState = new Map<string, number[]>()

function _rateLimited(sourceIp: string): boolean {
  const now = Date.now()
  const bucket = _rateState.get(sourceIp) ?? []
  const cutoff = now - _RATE_WINDOW_MS
  const fresh = bucket.filter((t) => t >= cutoff)
  if (fresh.length >= _RATE_LIMIT) {
    _rateState.set(sourceIp, fresh)
    return true
  }
  fresh.push(now)
  _rateState.set(sourceIp, fresh)
  return false
}

function _sigFingerprint(sig: string | null): string {
  if (!sig) return 'none'
  return createHash('sha256').update(sig).digest('hex').slice(0, 8)
}

/**
 * Twilio inbound WhatsApp webhook (VT-3.3b + VT-81 hardening).
 *
 * Deterministic ingress only (Pillar 1): verify the Twilio signature, then
 * forward the raw fields to the orchestrator.
 *
 * Pillar 7: never returns 5xx for an application error — 5xx triggers Twilio
 * retry + duplicate processing. 403 for bad signature; 429 for rate limit.
 *
 * VT-81 additive hardening:
 *   - Rate limit: 30 req/min per source IP; 429 on burst
 *   - Replay defense: MessageSid INSERT into `twilio_inbound_replay`
 *     ON CONFLICT DO NOTHING; duplicate → 200 + no orchestrator forward
 *   - PII redact: every console line through `redactForLog`
 *     (E.164 phones, Twilio SIDs, ≥7-digit runs, emails)
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const sourceIp =
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ?? 'unknown'

  if (_rateLimited(sourceIp)) {
    console.warn(
      redactForLog(JSON.stringify({ event: 'twilio_rate_limited', source_ip: sourceIp })),
    )
    return new NextResponse('too many requests', { status: 429 })
  }

  const rawBody = await request.text()
  const signature = request.headers.get('x-twilio-signature')
  const webhookUrl = process.env.TEAM_TWILIO_WEBHOOK_URL ?? request.url
  const params = parseTwilioBody(rawBody)

  if (!verifyTwilioSignature(signature, webhookUrl, params)) {
    console.warn(
      redactForLog(
        JSON.stringify({
          event: 'twilio_sig_invalid',
          source_ip: sourceIp,
          sig_fingerprint: _sigFingerprint(signature),
        }),
      ),
    )
    return new NextResponse('forbidden', { status: 403 })
  }

  const messageSid = params.MessageSid ?? ''

  // Replay defense — atomic INSERT ON CONFLICT DO NOTHING.
  if (messageSid) {
    try {
      const supabase = serverSecretClient()
      const { error } = await supabase.from('twilio_inbound_replay').insert({
        message_sid: messageSid,
        source_ip: sourceIp,
        signature_first_8: _sigFingerprint(signature),
      })
      if (error) {
        const code = (error as { code?: string }).code
        if (code === '23505' || /duplicate key/i.test(error.message ?? '')) {
          // Replay: same MessageSid landed within the rolling window.
          console.warn(
            redactForLog(
              JSON.stringify({
                event: 'twilio_replay_rejected',
                message_sid: messageSid,
                source_ip: sourceIp,
              }),
            ),
          )
          return twimlOk()
        }
        // Other DB errors: continue with orchestrator forward; don't
        // 5xx Twilio over a metadata-table problem.
        console.error(
          redactForLog(
            JSON.stringify({
              event: 'twilio_replay_db_error',
              reason: error.message,
            }),
          ),
        )
      }
    } catch (err) {
      console.error(
        redactForLog(
          JSON.stringify({
            event: 'twilio_replay_db_unreachable',
            reason: err instanceof Error ? err.message : 'unknown',
          }),
        ),
      )
    }
  }

  const twilioFields: Record<string, string> = {
    From: params.From ?? '',
    To: params.To ?? '',
    Body: params.Body ?? '',
    MessageSid: messageSid,
    NumMedia: params.NumMedia ?? '0',
    MediaUrl0: params.MediaUrl0 ?? '',
    MessageStatus: params.MessageStatus ?? '',
  }

  const result = await forwardToOrchestrator(twilioFields)
  if (!result.ok) {
    console.error(
      redactForLog(
        JSON.stringify({
          event: 'orchestrator_forward_failed',
          reason: result.reason,
          message_sid: messageSid,
        }),
      ),
    )
  }
  return twimlOk()
}

// VT-81 test seam — exposes the rate-limit map reset for canary tests.
export function _resetRateStateForTests(): void {
  _rateState.clear()
}
