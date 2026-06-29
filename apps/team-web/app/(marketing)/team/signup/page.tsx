import { redirect } from 'next/navigation'

import { DeployStamp } from '@/components/deploy-stamp'
import { launchMode } from '@/lib/launch-mode'

import { SignupForm } from './signup-form'

/**
 * VT-97 — mode-gate the signup route (server-side, build-time toggle). Only `live` shows the
 * full signup form; in `waitlist` or `maintenance` the landing (/team) owns the surface (the
 * waitlist form / maintenance notice lives there), so redirect there. Keeps the mode decision
 * out of the client form (no hydration mismatch).
 *
 * VT-508: DeployStamp rendered alongside SignupForm (page.tsx is a server component, so both
 * a client child (SignupForm) and a server child (DeployStamp) can coexist here).
 */
export default async function SignupPage() {
  if (launchMode() !== 'live') redirect('/team')
  return (
    <>
      <SignupForm />
      <DeployStamp />
    </>
  )
}
