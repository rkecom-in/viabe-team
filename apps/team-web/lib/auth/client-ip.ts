/**
 * VT-394 — trusted client-IP resolution for abuse / rate-limit keys.
 *
 * The LEFTMOST `x-forwarded-for` entry is client-supplied and SPOOFABLE: each
 * proxy in the chain APPENDS its address, so an attacker can prepend an
 * arbitrary IP to mint a fresh per-IP rate-limit bucket on every request and
 * fully evade the cap. The platform-set `x-vercel-forwarded-for` / `x-real-ip`
 * headers are written by Vercel's edge and overwrite any inbound value, so they
 * are the only trustworthy signal for a per-IP key. Prefer them; fall back to
 * the RIGHTMOST XFF hop (the platform-appended entry, not the client-supplied
 * leftmost one) only when neither is present; 'unknown' last.
 *
 * Use this for EVERY security-sensitive per-IP key (the owner OTP throttle —
 * whose orchestrator-side copy is the authoritative cap — plus the signup /
 * waitlist abuse guards). Do NOT read the raw leftmost XFF for a rate-limit key.
 */
export function trustedClientIp(req: Request): string {
  const h = req.headers

  const vercelIp = h.get('x-vercel-forwarded-for')?.trim()
  if (vercelIp) return vercelIp

  const realIp = h.get('x-real-ip')?.trim()
  if (realIp) return realIp

  // Last resort: the rightmost XFF entry is the platform-appended hop; the
  // leftmost is client-controlled and must never key a rate limit.
  const xff = h.get('x-forwarded-for')
  if (xff) {
    const hops = xff
      .split(',')
      .map((p) => p.trim())
      .filter(Boolean)
    const lastHop = hops.at(-1)
    if (lastHop) return lastHop
  }

  return 'unknown'
}
