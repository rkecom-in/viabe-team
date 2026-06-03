'use client'

/**
 * VT-297 — Telegram link-code issuer (client). VTR taps "Generate code" → server mints a single-use
 * code onto their own operator_telegram row → display it with /link instructions. The code is the
 * secret that binds the VTR's Telegram account; it's shown once here, used once via the bot.
 */

import { useState, useTransition } from 'react'

import { generateLinkCodeAction } from '@/app/(app)/team/ops/telegram/actions'

export function TelegramLink() {
  const [pending, startTransition] = useTransition()
  const [code, setCode] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  function generate() {
    startTransition(async () => {
      const res = await generateLinkCodeAction()
      if (res.ok && res.code) {
        setCode(res.code)
        setErr(null)
      } else {
        setErr(res.reason)
        setCode(null)
      }
    })
  }

  return (
    <div data-telegram-link>
      <p>
        Connect your Telegram so you can run ops from your phone. Generate a one-time code, then send{' '}
        <code>/link &lt;code&gt;</code> to the Viabe ops bot.
      </p>
      <button type="button" disabled={pending} onClick={generate}>
        Generate link code
      </button>
      {code && (
        <p data-link-code>
          Your code (single-use): <strong>{code}</strong>
          <br />
          In the bot, send: <code>/link {code}</code>
        </p>
      )}
      {err && <p data-link-error>Couldn&apos;t generate a code: {err}</p>}
    </div>
  )
}
