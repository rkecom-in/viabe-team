import { redirect } from 'next/navigation'

import { launchMode } from '@/lib/launch-mode'

import { SignupForm } from './signup-form'

/**
 * VT-97 — mode-gate the signup route (server-side, build-time toggle). Only `live` shows the
 * full signup form; in `waitlist` or `maintenance` the landing (/team) owns the surface (the
 * waitlist form / maintenance notice lives there), so redirect there. Keeps the mode decision
 * out of the client form (no hydration mismatch).
 */
export default function SignupPage() {
  if (launchMode() !== 'live') redirect('/team')
  return <SignupForm />
}
