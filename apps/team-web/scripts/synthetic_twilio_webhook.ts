#!/usr/bin/env tsx
/**
 * Tier 2 dev testing — synthetic Twilio webhook fixture (VT-3.3b, CL-67).
 *
 * Constructs a Twilio-shaped POST with a valid X-Twilio-Signature (via the
 * Twilio SDK) and fires it at a locally-running team-web webhook route. This
 * exercises the full chain: signature verification -> forward -> orchestrator
 * tenant lookup + rate limiting -> DBOS workflow start.
 *
 * Prerequisites: team-web running (`pnpm --filter @viabe/team-web dev`), the
 * orchestrator running, and TEAM_TWILIO_AUTH_TOKEN + INTERNAL_API_SECRET set.
 *
 * Usage:
 *   pnpm --filter @viabe/team-web exec tsx scripts/synthetic_twilio_webhook.ts \
 *     --tenant-phone "+919999999999" --body "STOP" --message-sid "SM_test_001"
 */
import twilio from 'twilio'

function arg(name: string, fallback: string): string {
  const idx = process.argv.indexOf(`--${name}`)
  const value = idx !== -1 ? process.argv[idx + 1] : undefined
  return value ?? fallback
}

async function main(): Promise<number> {
  const authToken = process.env.TEAM_TWILIO_AUTH_TOKEN
  if (!authToken) {
    console.error('error: TEAM_TWILIO_AUTH_TOKEN not set')
    return 1
  }
  const url =
    process.env.TEAM_TWILIO_WEBHOOK_URL ??
    'http://localhost:3000/api/team/twilio/webhook'

  const params: Record<string, string> = {
    From: arg('tenant-phone', '+919999999999'),
    To: '+910000000000',
    Body: arg('body', 'hello'),
    MessageSid: arg('message-sid', `SM_test_${Date.now()}`),
    NumMedia: arg('num-media', '0'),
  }

  const signature = twilio.getExpectedTwilioSignature(authToken, url, params)
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'content-type': 'application/x-www-form-urlencoded',
      'x-twilio-signature': signature,
    },
    body: new URLSearchParams(params).toString(),
  })
  console.log(`HTTP ${res.status}`)
  console.log(await res.text())
  return res.status === 200 ? 0 : 1
}

main().then((code) => process.exit(code))
