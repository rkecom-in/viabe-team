import { createHmac, timingSafeEqual } from 'crypto'

/**
 * Verify a Razorpay webhook HMAC-SHA256 signature (VT-89).
 *
 * Razorpay sends `hex(HMAC-SHA256(secret, rawBody))` in the `x-razorpay-signature`
 * header. We recompute over the RAW request body (not the re-serialised JSON) and
 * compare in constant time. An unverified webhook is NEVER trusted — it drives
 * money (fees + phase transitions). Returns false on any missing input or length
 * mismatch.
 */
export function verifyRazorpaySignature(
  signature: string | null,
  rawBody: string,
  secret: string,
): boolean {
  if (!signature || !secret) return false
  const expected = createHmac('sha256', secret).update(rawBody).digest('hex')
  const sigBuf = Buffer.from(signature)
  const expBuf = Buffer.from(expected)
  if (sigBuf.length !== expBuf.length) return false
  return timingSafeEqual(sigBuf, expBuf)
}
