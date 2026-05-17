import twilio from 'twilio'

/** Parse an `application/x-www-form-urlencoded` body into a flat record. */
export function parseTwilioBody(raw: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of new URLSearchParams(raw)) {
    out[key] = value
  }
  return out
}

/**
 * Verify an `X-Twilio-Signature` against the auth token, the public webhook
 * URL, and the POST params — via the official Twilio SDK (Pillar 8: no
 * bespoke crypto). Returns false when the token or signature is missing.
 */
export function verifyTwilioSignature(
  signature: string | null,
  url: string,
  params: Record<string, string>,
): boolean {
  const authToken = process.env.TWILIO_AUTH_TOKEN
  if (!authToken || !signature) {
    return false
  }
  return twilio.validateRequest(authToken, signature, url, params)
}
