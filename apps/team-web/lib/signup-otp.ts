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
  | { ok: true }
  | { ok: false; error: 'rate_limited' | 'invalid_code' | 'duplicate' | 'generic' }

/**
 * Step 2 — verify the OTP → get the pre-tenant verified-number token → create the tenant with
 * `Authorization: Bearer <token>`. Invalid vs expired are NOT distinguished (both → invalid_code,
 * no enumeration). A missing token is treated the same. The token is only ever a header.
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
  if (!vres.ok) return { ok: false, error: 'invalid_code' } // invalid OR expired — generic
  const { token } = (await vres.json().catch(() => ({}))) as { token?: string }
  if (!token) return { ok: false, error: 'invalid_code' }

  const res = await f('/api/team/signup', {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
    body: JSON.stringify(payload),
  })
  if (res.status === 201) return { ok: true }
  const body = await res.json().catch(() => ({}))
  return { ok: false, error: body?.detail?.code === 'duplicate' ? 'duplicate' : 'generic' }
}
