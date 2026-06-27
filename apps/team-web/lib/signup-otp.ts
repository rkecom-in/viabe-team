/**
 * VT-96 — the signup OTP network flow, extracted from the form component so the security-
 * relevant logic (the Bearer-token threading + the generic, non-enumerating error mapping) is
 * unit-testable in the node test env (no jsdom). The component maps these results to bilingual
 * messages + step transitions; this module owns the fetch sequence only.
 *
 * CL-390: no PII is logged here — the phone/code/token flow straight to the proxy.
 */
type Fetch = typeof fetch

export type OtpRequestResult = { ok: true } | { ok: false; error: 'rate_limited' | 'generic' }

/** Step 1 — request an OTP to the WhatsApp number (the VT-326 proof-of-control gate). */
export async function requestSignupOtp(phone: string, f: Fetch = fetch): Promise<OtpRequestResult> {
  const res = await f('/api/team/auth/request-otp', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ phone }),
  })
  if (res.status === 429) return { ok: false, error: 'rate_limited' }
  if (!res.ok) return { ok: false, error: 'generic' }
  return { ok: true }
}

export type CreateResult =
  // VT-411: the create response carries the NEW tenant_id so the POST-create ownership step can
  // flip owner_channel_verified on the REAL tenant (a pre-create tenant_id='' would be a no-op).
  | { ok: true; tenantId: string | null }
  // `verify_unavailable` (sweep #8): a transient verify-service outage (502) — retryable "on our
  // side", NOT "code invalid". `gst_reject` / `vendor_down` (sweep #11): the create-step GST gate
  // copy — a 422 reject vs a 503 retryable vendor outage, each carrying the orchestrator's authored
  // bilingual `message` (gate_copy()) so the form renders it instead of collapsing to one generic.
  | {
      ok: false
      error:
        | 'rate_limited'
        | 'invalid_code'
        | 'verify_unavailable'
        | 'duplicate'
        | 'gst_reject'
        | 'vendor_down'
        | 'generic'
      message?: string
    }

/**
 * Step 2 — verify the OTP → get the pre-tenant verified-number token → create the tenant with
 * `Authorization: Bearer <token>`. Invalid vs expired are NOT distinguished (both → invalid_code,
 * no enumeration). A missing token is treated the same. The token is only ever a header.
 *
 * VT-411: on a 201 the orchestrator returns the new tenant_id (signup.py) — thread it out so the
 * POST-create ownership step targets the REAL tenant. tenant_id is an opaque id, not PII (CL-390).
 */
export async function verifyOtpAndCreate(
  payload: Record<string, unknown> & { whatsapp_number: string },
  code: string,
  f: Fetch = fetch,
): Promise<CreateResult> {
  const vres = await f('/api/team/auth/verify-otp-for-signup', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ phone: payload.whatsapp_number, code }),
  })
  if (vres.status === 429) return { ok: false, error: 'rate_limited' }
  // Sweep #8: a 502 from the verify route means the verify SERVICE is down (the route fails closed
  // with 502 on a verify-check error) — NOT that the code is wrong. Surface it as a retryable "on
  // our side" state, distinct from the 401/expired path (which carries no code-validity tell, so it
  // stays collapsed to invalid_code — anti-enumeration preserved).
  if (vres.status === 502) return { ok: false, error: 'verify_unavailable' }
  if (!vres.ok) return { ok: false, error: 'invalid_code' } // invalid OR expired — generic
  const { token } = (await vres.json().catch(() => ({}))) as { token?: string }
  if (!token) return { ok: false, error: 'invalid_code' }

  const res = await f('/api/team/signup', {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
    body: JSON.stringify(payload),
  })
  const body = (await res.json().catch(() => ({}))) as {
    tenant_id?: unknown
    detail?: { code?: string; message?: string }
  }
  if (res.status === 201) {
    return { ok: true, tenantId: typeof body?.tenant_id === 'string' ? body.tenant_id : null }
  }
  // Sweep #11: branch on the create-step gate so the orchestrator's authored (already EN/HI-resolved
  // via gate_copy()) `detail.message` reaches the form, instead of discarding it into one generic.
  // A 422 invalid_gstin is a TERMINAL "GST-registered only" reject; a 503 vendor_down is a RETRYABLE
  // outage — visibly distinct. Everything else stays duplicate (409) / generic.
  const detailCode = body?.detail?.code
  const message = typeof body?.detail?.message === 'string' ? body.detail.message : undefined
  // Sweep #7: a 429 (or detail.code 'rate_limited') on create is the signup-create rate limit — map
  // it to rate_limited (the wait copy) BEFORE the duplicate/generic fallthrough, mirroring the verify
  // leg. Keyed on code too, but NOT swallowing a disabled-signup 404 (code 'not_enabled') → generic.
  if (res.status === 429 || detailCode === 'rate_limited') return { ok: false, error: 'rate_limited' }
  if (res.status === 422 && detailCode === 'invalid_gstin') {
    return { ok: false, error: 'gst_reject', message }
  }
  if (res.status === 503 && detailCode === 'vendor_down') {
    return { ok: false, error: 'vendor_down', message }
  }
  return { ok: false, error: detailCode === 'duplicate' ? 'duplicate' : 'generic' }
}
