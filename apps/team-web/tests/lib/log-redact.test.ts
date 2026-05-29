/**
 * VT-81 — log redaction helper tests.
 *
 * Covers the patterns the Twilio webhook handler emits:
 *   - E.164 phone
 *   - Twilio MessageSid (SMxxxx)
 *   - Email
 *   - Bare digit run ≥ 7
 */

import { describe, expect, it } from 'vitest'

import { redactForLog } from '@/lib/log-redact'

describe('VT-81 — redactForLog', () => {
  it('redacts E.164 phones to phone_tok_<hash>', () => {
    const out = redactForLog('Reply from +919876543210 OK')
    expect(out).not.toContain('+919876543210')
    expect(out).toMatch(/phone_tok_[a-f0-9]{8}/)
  })

  it('redacts Twilio SMxxxx SIDs', () => {
    const sid = 'SM' + 'a'.repeat(32)
    const out = redactForLog(`MessageSid=${sid}`)
    expect(out).not.toContain(sid)
    expect(out).toContain('SM_REDACTED')
  })

  it('redacts emails to email_<hash>', () => {
    const out = redactForLog('Contact fazal@viabe.ai for support')
    expect(out).not.toContain('fazal@viabe.ai')
    expect(out).toMatch(/email_[a-f0-9]{8}/)
  })

  it('redacts bare digit runs ≥ 7', () => {
    const out = redactForLog('Account 1234567890 transferred')
    expect(out).toContain('[REDACTED_DIGITS]')
    expect(out).not.toContain('1234567890')
  })

  it('preserves non-PII text + redacts mixed', () => {
    const out = redactForLog('User +919000000000 wrote: hello world')
    expect(out).toContain('hello world')
    expect(out).toContain('phone_tok_')
    expect(out).not.toContain('+919000000000')
  })
})
