/**
 * VT-81 — minimal team-web log redactor.
 *
 * Ports the load-bearing patterns from
 * `orchestrator/observability/pii.py::redact_for_log`:
 *   - E.164 phones (+91XXXXXXXXXX) → `phone_tok_<8-char sha256>`
 *   - Twilio SIDs (SMxxxx / MMxxxx / MKxxxx) → `<sid_kind>_REDACTED`
 *   - Bare digit runs ≥ 7 → `[REDACTED_DIGITS]`
 *   - Email addresses → `email_<8-char sha256>`
 *
 * NOT a fully PII-safe redactor — covers the specific surfaces the
 * Twilio webhook handler logs. Future surfaces extend by adding
 * patterns here, NOT by inlining per-route regex.
 */

import { createHash } from 'crypto'

const PHONE_E164 = /\+\d{10,15}/g
const TWILIO_SID = /\b(SM|MM|MK|SK|CA)[a-f0-9]{32}\b/gi
const BARE_DIGITS = /\b\d{7,}\b/g
const EMAIL = /\b[\w.+-]+@[\w.-]+\.\w+\b/g

function hash8(s: string): string {
  return createHash('sha256').update(s, 'utf8').digest('hex').slice(0, 8)
}

export function redactForLog(input: string): string {
  return input
    .replace(PHONE_E164, (m) => `phone_tok_${hash8(m)}`)
    .replace(TWILIO_SID, (m) => `${m.slice(0, 2).toUpperCase()}_REDACTED`)
    .replace(EMAIL, (m) => `email_${hash8(m)}`)
    .replace(BARE_DIGITS, '[REDACTED_DIGITS]')
}

export function redactObject<T extends Record<string, unknown>>(obj: T): T {
  const out = {} as Record<string, unknown>
  for (const [k, v] of Object.entries(obj)) {
    if (typeof v === 'string') {
      out[k] = redactForLog(v)
    } else if (v && typeof v === 'object' && !Array.isArray(v)) {
      out[k] = redactObject(v as Record<string, unknown>)
    } else {
      out[k] = v
    }
  }
  return out as T
}
