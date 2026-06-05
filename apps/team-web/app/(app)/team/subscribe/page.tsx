/** Card-capture / subscribe page at trial→paid conversion (VT-91). */

import { redirect } from 'next/navigation'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { TrialEndTokenError, verifyTrialEndToken } from '@/lib/auth/verify-trial-end-token'

import { RazorpayCheckout } from './RazorpayCheckout'

export const dynamic = 'force-dynamic'

const PLAN_LABELS: Record<string, string> = {
  founding: 'Founding',
  standard: 'Standard',
  pro: 'Pro',
}

/**
 * Auth: a logged-in portal owner (session cookie) OR a trial-end deep-link token
 * (`?token=`). No card capture without one. The page authenticates ONLY to gate
 * rendering; the actual tenant is re-derived server-side at POST /api/team/razorpay/
 * subscribe (never trusts the client). Card details are entered on Razorpay's hosted
 * form — we never see or store them (PCI SAQ-A; docs/team/pci-posture.md).
 */
export default async function SubscribePage({
  searchParams,
}: {
  searchParams: Promise<{ plan?: string; token?: string }>
}) {
  const { plan, token } = await searchParams
  const planTier = plan && plan in PLAN_LABELS ? plan : 'standard'

  let authed = false
  try {
    await requireOwnerSession()
    authed = true
  } catch (err) {
    if (!(err instanceof OwnerUnauthorizedError)) throw err
  }
  if (!authed && token) {
    try {
      await verifyTrialEndToken(token)
      authed = true
    } catch (err) {
      if (!(err instanceof TrialEndTokenError)) throw err
    }
  }
  if (!authed) redirect('/team/login?next=/team/subscribe')

  return (
    <main data-area="subscribe">
      <h1>Subscribe — {PLAN_LABELS[planTier]}</h1>
      <p>
        Add a payment method to continue after your trial. Card details are entered on
        Razorpay&apos;s secure form — we never see or store them.
      </p>
      <RazorpayCheckout planTier={planTier} token={token ?? null} />
    </main>
  )
}
