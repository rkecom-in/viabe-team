'use client'

import Script from 'next/script'
import { useState } from 'react'

interface Props {
  planTier: string
  /** Trial-end deep-link token (forwarded so the API can auth the deep-link path). */
  token: string | null
}

interface RazorpayInstance {
  open: () => void
}
interface RazorpayOptions {
  key?: string
  subscription_id: string
  name: string
  handler: () => void
}
declare global {
  interface Window {
    Razorpay?: new (opts: RazorpayOptions) => RazorpayInstance
  }
}

type Status = 'idle' | 'creating' | 'checkout_open' | 'created' | 'done' | 'error'

const STATUS_TEXT: Record<Status, string> = {
  idle: '',
  creating: 'Creating your subscription…',
  checkout_open: 'Opening Razorpay secure checkout…',
  created: 'Subscription created — add your card to finish.',
  done: 'All set — your subscription is active.',
  error: 'Something went wrong. Please try again.',
}

/**
 * VT-91 card capture. On Subscribe: (1) POST /api/team/razorpay/subscribe — the
 * orchestrator (money-authoritative) creates the Razorpay subscription + returns its id;
 * the tenant is re-derived SERVER-SIDE (we send only plan_tier + the deep-link token).
 * (2) Open Razorpay's HOSTED checkout with that subscription_id to capture the card — we
 * never see card data (PCI SAQ-A). The first charge fires the VT-89 `payment.captured`
 * webhook, which is what flips the phase to paid_active (conversion stays webhook-only).
 *
 * The Razorpay key (`NEXT_PUBLIC_RAZORPAY_KEY_ID`) is a publishable id, NEEDS-FAZAL for
 * LIVE. If Checkout.js / the key is absent (dev/stub), the subscription is still created;
 * the hosted card step is simply skipped.
 */
export function RazorpayCheckout({ planTier, token }: Props) {
  const [status, setStatus] = useState<Status>('idle')

  async function onSubscribe(): Promise<void> {
    setStatus('creating')
    let res: Response
    try {
      res = await fetch('/api/team/razorpay/subscribe', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ plan_tier: planTier, ...(token ? { token } : {}) }),
      })
    } catch {
      setStatus('error')
      return
    }
    if (!res.ok) {
      setStatus('error')
      return
    }
    const data = (await res.json()) as { razorpaySubscriptionId: string | null }
    if (!data.razorpaySubscriptionId) {
      // The subscription was NOT created — a real failure, not a skipped card step.
      setStatus('error')
      return
    }
    const keyId = process.env.NEXT_PUBLIC_RAZORPAY_KEY_ID
    if (!window.Razorpay || !keyId) {
      // Subscription created; hosted card capture unavailable here (dev/stub / no key).
      setStatus('created')
      return
    }
    const rzp = new window.Razorpay({
      key: keyId,
      subscription_id: data.razorpaySubscriptionId,
      name: 'Viabe Team',
      handler: () => setStatus('done'),
    })
    rzp.open()
    setStatus('checkout_open')
  }

  return (
    <div data-component="razorpay-checkout">
      <Script src="https://checkout.razorpay.com/v1/checkout.js" strategy="lazyOnload" />
      <button
        type="button"
        data-action="subscribe"
        onClick={() => void onSubscribe()}
        disabled={status === 'creating'}
      >
        Subscribe
      </button>
      <p data-status={status}>{STATUS_TEXT[status]}</p>
    </div>
  )
}
