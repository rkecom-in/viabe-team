/**
 * VT-250 — owner-portal login: PHONE ENTRY step.
 *
 * Owner enters their mobile → POST /api/team/auth/request-otp (Twilio Verify
 * over WhatsApp, the live channel) → advance to the code-entry step. The
 * response is intentionally generic ({ sent: true } regardless of whether the
 * phone maps to a tenant) so we never leak tenant existence here.
 *
 * Client component: the two-step flow (phone → code) is held in local state;
 * the actual auth happens server-side in the API routes.
 *
 * 2026-08-21 — restyled to the Viabe design scheme from the Claude Design
 * "Viabe Reports" project (`Signin Flow.dc.html`). Presentation only: the request,
 * the generic response handling and the query-string handoff to the code step are
 * unchanged, as are the e2e hooks (`data-area`, `data-step`, `data-element`,
 * `data-state`). The prototype's "no account uses this number" screen is deliberately
 * NOT implemented — see signin-copy.ts; it would leak the tenant existence this
 * endpoint is generic to protect.
 */

'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import Image from 'next/image'

import * as ui from '@/lib/viabe-ui'
import { type Lang, ts } from './signin-copy'

const PHONE_RE = /^[6-9]\d{9}$/

export default function OwnerLoginPage() {
  const router = useRouter()
  const [lang, setLang] = useState<Lang>('en')
  const [phone, setPhone] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!phone) {
      setError(ts(lang, 'errRequired'))
      return
    }
    if (!PHONE_RE.test(phone)) {
      setError(ts(lang, 'errPhone'))
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch('/api/team/auth/request-otp', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string }
        setError(data.error ?? ts(lang, 'sendFailed'))
        return
      }
      // Carry the entered phone to the code step (never persisted server-side
      // between steps — re-sent on verify). Encoded in the query string.
      router.push(`/team/login/code?phone=${encodeURIComponent(phone)}`)
    } catch {
      setError(ts(lang, 'networkError'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={`${ui.page} flex flex-col`} data-area="team-owner-login">
      <header className={ui.header}>
        <Image
          src="/brand/header-logo-light.webp"
          alt="Viabe"
          width={173}
          height={34}
          priority
          className="h-[34px] w-auto"
        />
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{ts(lang, 'langLabel')}</span>
          <div className={ui.langToggle}>
            <button type="button" onClick={() => setLang('en')} aria-pressed={lang === 'en'} className={ui.langButton(lang === 'en')}>
              English
            </button>
            <button type="button" onClick={() => setLang('hi')} aria-pressed={lang === 'hi'} className={ui.langButton(lang === 'hi')}>
              हिन्दी
            </button>
          </div>
        </div>
      </header>

      <main className="flex flex-1 items-start justify-center px-4 py-6 sm:items-center sm:px-8 sm:py-12">
        <div className="flex w-full max-w-[460px] flex-col gap-5">
          <div className="flex flex-col gap-2">
            <h1 className={`${ui.h1} text-2xl sm:text-[28px]`}>{ts(lang, 'h1')}</h1>
            <p className={ui.body}>{ts(lang, 'sub')}</p>
          </div>

          <section className={ui.panel}>
            <form onSubmit={onSubmit} className="flex flex-col gap-4.5" data-step="phone">
              <label className={ui.fieldLabel} htmlFor="phone">
                <span className={ui.fieldLabelText}>{ts(lang, 'phoneLabel')}</span>
                <span className="flex items-stretch">
                  <span className={ui.phonePrefix}>+91</span>
                  <input
                    id="phone"
                    name="phone"
                    type="tel"
                    inputMode="numeric"
                    autoComplete="tel-national"
                    required
                    value={phone}
                    onChange={(e) => { setPhone(e.target.value.replace(/\D/g, '').slice(0, 10)); setError(null) }}
                    placeholder="98765 43210"
                    className={ui.phoneField(Boolean(error))}
                  />
                </span>
                <span className={ui.hint}>{ts(lang, 'phoneHint')}</span>
              </label>

              {error ? (
                <div data-state="error" role="alert" className={ui.alertError}>
                  <p className={ui.alertBody}>{error}</p>
                </div>
              ) : null}

              <button
                type="submit"
                disabled={submitting}
                data-element="send-code-button"
                className={ui.primaryButton(submitting)}
              >
                {submitting ? ts(lang, 'sending') : ts(lang, 'sendCta')}
              </button>

              <p className={`${ui.hint} text-center`}>{ts(lang, 'noPassword')}</p>
            </form>
          </section>

          <p className={`${ui.bodySm} text-center`}>
            {ts(lang, 'newHere')}{' '}
            <a href="/team/signup" className="font-semibold text-ink-primary underline">
              {ts(lang, 'createAccount')}
            </a>
          </p>
          <p className={`${ui.hint} text-center`}>{ts(lang, 'footer')}</p>
        </div>
      </main>
    </div>
  )
}
