/**
 * VT-411 — the signup OWNERSHIP-verification flow, extracted from the wizard component so the
 * decision logic (the browser→proxy fetch sequence + the DIN format gate) is unit-testable in the
 * node test env (no jsdom — the repo's pattern, mirroring lib/entity-match.ts + lib/signup-otp.ts).
 * The component maps these results to bilingual copy + sub-step transitions; this module owns the
 * fetch sequence + the format gate ONLY.
 *
 * Fazal's bar: after the entity verifies (gstin_verified), the owner proves they OWN the business via
 * a DISTINCT OTP to the DISCOVERED PUBLIC business number (NOT the personal WhatsApp the signup OTP
 * already proved) — observably its own step. A DIN verify is offered alongside. owner_channel_verified
 * is the SOLE signal that ownership is proven; a vendor failure NEVER fakes it (fail-closed).
 *
 * CL-390: no business identity (public_phone / din / cin / code) is logged here — values flow straight
 * to the server-side proxy routes (which forward to the orchestrator under X-Internal-Secret).
 */
type Fetch = typeof fetch

export interface OwnershipOtpStartResult {
  ok: boolean
  /** pending | invalid_request | http_<n> | timeout | error — render-only; never implies proof. */
  status: string
}

export interface OwnershipVerifyResult {
  /** True iff owner_channel_verified came back true (the SOLE signal ownership is proven). */
  ownerChannelVerified: boolean
  /** ok | invalid_code | invalid_request | http_<n> | timeout | error — render-only. */
  reason: string
}

/**
 * Step A1 — start the ownership OTP via the server-side proxy route. The route holds the
 * INTERNAL_API_SECRET and forwards to the orchestrator; the browser only ever talks to /api/team.
 * Fail-CLOSED on transport: any throw → {ok:false, status:'error'} (the owner can fall back to DIN).
 */
export async function startOwnershipOtp(
  tenantId: string,
  publicPhone: string,
  f: Fetch = fetch,
): Promise<OwnershipOtpStartResult> {
  try {
    const res = await f('/api/team/onboard/ownership/otp/start', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, public_phone: publicPhone }),
    })
    if (!res.ok) return { ok: false, status: `http_${res.status}` }
    const data = (await res.json().catch(() => ({}))) as { ok?: boolean; status?: string }
    return { ok: Boolean(data.ok), status: data.status ?? 'pending' }
  } catch {
    return { ok: false, status: 'error' }
  }
}

/**
 * Step A2 — confirm the ownership OTP via the server-side proxy route. owner_channel_verified is the
 * ONLY thing that proves ownership. Fail-CLOSED on transport: any throw →
 * {ownerChannelVerified:false} (never a faked proven owner).
 */
export async function confirmOwnershipOtp(
  tenantId: string,
  publicPhone: string,
  code: string,
  f: Fetch = fetch,
): Promise<OwnershipVerifyResult> {
  try {
    const res = await f('/api/team/onboard/ownership/otp/confirm', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, public_phone: publicPhone, code }),
    })
    if (!res.ok) return { ownerChannelVerified: false, reason: `http_${res.status}` }
    const data = (await res.json().catch(() => ({}))) as {
      owner_channel_verified?: boolean
      reason?: string
    }
    return {
      ownerChannelVerified: Boolean(data.owner_channel_verified),
      reason: data.reason ?? 'ok',
    }
  } catch {
    return { ownerChannelVerified: false, reason: 'error' }
  }
}

/**
 * Step B (alternative) — verify ownership via DIN through the server-side proxy route. Fail-CLOSED on
 * transport: any throw → {ownerChannelVerified:false} (never a faked proven owner). `reason` is the
 * owner's free-text note (forwarded for the orchestrator's audit; never logged here).
 */
export async function verifyOwnerViaDin(
  tenantId: string,
  din: string,
  cin: string,
  reason: string,
  f: Fetch = fetch,
): Promise<OwnershipVerifyResult> {
  try {
    const res = await f('/api/team/onboard/ownership/din', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, din, cin, reason }),
    })
    if (!res.ok) return { ownerChannelVerified: false, reason: `http_${res.status}` }
    const data = (await res.json().catch(() => ({}))) as {
      owner_channel_verified?: boolean
      reason?: string
    }
    return {
      ownerChannelVerified: Boolean(data.owner_channel_verified),
      reason: data.reason ?? 'ok',
    }
  } catch {
    return { ownerChannelVerified: false, reason: 'error' }
  }
}

/**
 * VT-411 — client-side DIN FORMAT pre-check: a Director Identification Number is exactly 8 digits.
 * This is a format gate ONLY (lets the owner fix a typo before we round-trip the registry) — it is
 * NOT verification; the authoritative gate stays the orchestrator's registry check
 * (owner_channel_verified). Input is trimmed before the test.
 */
export function isValidDinFormat(din: string): boolean {
  return /^\d{8}$/.test((din || '').trim())
}

/**
 * VT-411 — client-side public-phone FORMAT pre-check for the owner-entered fallback (when the
 * discovered number is absent). Mirrors the signup phone gate: a +91 mobile (+91 then 6-9 then 9
 * digits). Format gate ONLY — the OTP round-trip is the real proof. Input is trimmed before the test.
 */
export function isValidPublicPhoneFormat(phone: string): boolean {
  return /^\+91[6-9]\d{9}$/.test((phone || '').trim())
}
