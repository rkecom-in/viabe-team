/**
 * VT-250 — owner-portal login: CODE ENTRY step.
 *
 * Owner enters the OTP delivered over WhatsApp → POST /api/team/auth/verify-otp
 * with { phone, code }. On approval the server resolves owner_phone → tenant,
 * mints the viabe_team_session cookie, and returns a redirect target. A denied
 * code, an unknown phone, or a verify error all return a generic failure.
 *
 * The phone is carried from the phone-entry step via the ?phone= query param
 * (re-sent on verify; never persisted server-side between steps).
 *
 * 2026-08-21 — restyled to the Viabe design scheme (`Signin Flow.dc.html`).
 * Presentation only: the verify call, the generic-failure handling, the phone-less
 * redirect guard and the e2e hooks are unchanged. The prototype's three-strikes
 * lockout is NOT implemented — the server does not expose attempt counts, and a
 * client-side counter would be both unenforced and a lie about where the limit lives.
 */

'use client'

import { Suspense, useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import Image from 'next/image'

import * as ui from '@/lib/viabe-ui'
import { type Lang, ts } from '../signin-copy'

function CodeEntryForm() {
  const router = useRouter()
  const params = useSearchParams()
  const phone = params.get('phone') ?? ''

  const [lang, setLang] = useState<Lang>('en')
  const [code, setCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Sweep #9: direct entry / param loss leaves a phone-less code form that can only 400 ("enter a
  // valid mobile number") with no field to fix it. Redirect back to the phone-entry step so the owner
  // re-enters their number, instead of stranding them on a dead form. router.replace (not push) so
  // back-navigation doesn't trap them on this phone-less page.
  useEffect(() => {
    if (!phone) router.replace('/team/login')
  }, [phone, router])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (code.trim().length < 6) {
      setError(ts(lang, 'errCodeShort'))
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch('/api/team/auth/verify-otp', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone, code }),
      })
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean
        redirect?: string
        error?: string
      }
      if (!res.ok || !data.ok) {
        setError(data.error ?? ts(lang, 'errCodeGeneric'))
        return
      }
      router.push(data.redirect ?? '/team/dashboard')
    } catch {
      setError(ts(lang, 'networkError'))
    } finally {
      setSubmitting(false)
    }
  }

  const digits = phone.replace(/\D/g, '').slice(-10)
  const phoneDisplay = `+91 ${digits.slice(0, 5)}${digits.length > 5 ? ' ' + digits.slice(5) : ''}`

  return (
    <div className={`${ui.page} flex flex-col`}>
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
            <p className={ui.body}>{ts(lang, 'sentGeneric')}</p>
          </div>

          <section className={ui.panel}>
            <form onSubmit={onSubmit} className="flex flex-col gap-4" data-step="code">
              <div className={`${ui.alertInfo} flex-row flex-wrap items-center justify-between`}>
                <span className="flex flex-col gap-0.5">
                  <span className="text-xs text-muted-foreground">{ts(lang, 'sentTo')}</span>
                  <span className={ui.monoValue}>{phoneDisplay}</span>
                </span>
                <button
                  type="button"
                  onClick={() => router.replace('/team/login')}
                  className={ui.linkButton()}
                >
                  {ts(lang, 'editNumber')}
                </button>
              </div>

              <label className={ui.fieldLabel} htmlFor="code">
                <span className={ui.fieldLabelText}>{ts(lang, 'codeLabel')}</span>
                <input
                  id="code"
                  name="code"
                  type="text"
                  inputMode="numeric"
                  maxLength={6}
                  autoComplete="one-time-code"
                  required
                  value={code}
                  onChange={(e) => { setCode(e.target.value.replace(/\D/g, '').slice(0, 6)); setError(null) }}
                  placeholder="––––––"
                  className={ui.codeField(Boolean(error))}
                />
              </label>

              {error ? (
                <div data-state="error" role="alert" className={ui.alertError}>
                  <p className={ui.alertBody}>{error}</p>
                </div>
              ) : null}

              <button
                type="submit"
                disabled={submitting}
                data-element="verify-code-button"
                className={ui.primaryButton(submitting)}
              >
                {submitting ? ts(lang, 'signingIn') : ts(lang, 'signInCta')}
              </button>

              <div className="flex flex-wrap items-center justify-between gap-3">
                <button
                  type="button"
                  onClick={() => router.push('/team/login')}
                  className={ui.linkButton()}
                >
                  {ts(lang, 'resend')}
                </button>
                <span className={ui.hint}>{ts(lang, 'expiry')}</span>
              </div>
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

export default function OwnerLoginCodePage() {
  return (
    <Suspense fallback={null}>
      <div data-area="team-owner-login-code">
        <CodeEntryForm />
      </div>
    </Suspense>
  )
}
