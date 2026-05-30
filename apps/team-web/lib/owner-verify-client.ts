/**
 * VT-250 — team-web → orchestrator Twilio Verify client.
 *
 * Mirrors `lib/orchestrator-client.ts` (INTERNAL_API_SECRET-signed POST). The
 * orchestrator owns the Twilio creds + Verify Service SID (defense-in-depth,
 * same isolation as the resolve-phone decrypt-proxy). team-web only passes the
 * phone + channel / code and receives a PII-safe status envelope.
 *
 * CL-390: the phone + code cross this boundary in the request body but the
 * orchestrator NEVER logs them; the response carries only verification_sid +
 * status. team-web never logs the phone/code either (see the routes).
 */

const _ORCHESTRATOR_DEFAULT = 'http://localhost:8001'
const _TIMEOUT_MS = 10_000

function _base(): string {
  return process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
}

function _secret(): string {
  return process.env.INTERNAL_API_SECRET ?? ''
}

export interface VerifyStartResult {
  ok: boolean
  status: string | null // 'pending' on success
  verificationSid: string | null
  /** ok | http_<n> | timeout | error */
  reason: string
}

export interface VerifyCheckResult {
  ok: boolean
  approved: boolean
  status: string | null // 'approved' | 'denied' | ...
  verificationSid: string | null
  reason: string
}

export async function startOwnerVerification(
  phoneE164: string,
  channel: string,
  tenantId: string | null,
): Promise<VerifyStartResult> {
  try {
    const res = await fetch(`${_base()}/api/orchestrator/owner/verify-start`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Secret': _secret(),
      },
      body: JSON.stringify({ phone: phoneE164, channel, tenant_id: tenantId }),
      signal: AbortSignal.timeout(_TIMEOUT_MS),
    })
    if (!res.ok) {
      return {
        ok: false,
        status: null,
        verificationSid: null,
        reason: `http_${res.status}`,
      }
    }
    const data = (await res.json()) as {
      status?: string
      verification_sid?: string
    }
    return {
      ok: true,
      status: data.status ?? null,
      verificationSid: data.verification_sid ?? null,
      reason: 'ok',
    }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return {
      ok: false,
      status: null,
      verificationSid: null,
      reason: timedOut ? 'timeout' : 'error',
    }
  }
}

export async function checkOwnerVerification(
  phoneE164: string,
  code: string,
  tenantId: string | null,
): Promise<VerifyCheckResult> {
  try {
    const res = await fetch(`${_base()}/api/orchestrator/owner/verify-check`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Secret': _secret(),
      },
      body: JSON.stringify({ phone: phoneE164, code, tenant_id: tenantId }),
      signal: AbortSignal.timeout(_TIMEOUT_MS),
    })
    if (!res.ok) {
      return {
        ok: false,
        approved: false,
        status: null,
        verificationSid: null,
        reason: `http_${res.status}`,
      }
    }
    const data = (await res.json()) as {
      approved?: boolean
      status?: string
      verification_sid?: string
    }
    return {
      ok: true,
      approved: data.approved === true,
      status: data.status ?? null,
      verificationSid: data.verification_sid ?? null,
      reason: 'ok',
    }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return {
      ok: false,
      approved: false,
      status: null,
      verificationSid: null,
      reason: timedOut ? 'timeout' : 'error',
    }
  }
}
