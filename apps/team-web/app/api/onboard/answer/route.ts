/** VT-211 — owner-answer ingest for the onboarding agent.
 *
 * POST handler. Validates the form input, forwards to the orchestrator's
 * /api/orchestrator/integrations/onboard-step endpoint (X-Internal-Secret
 * signed), and redirects the browser back to /team/onboard so the page
 * re-fetches state.
 */

import { redirect } from 'next/navigation'
import { NextResponse } from 'next/server'

import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'
import { forwardOnboardStep } from '@/lib/orchestrator-client'

export async function POST(request: Request): Promise<Response> {
  // VT-415: gated on the OWNER session; the tenant is derived SERVER-SIDE from
  // that session (never FAZAL_TENANT_ID, never a client field). An unauthed
  // POST bounces to the OWNER login (this is a same-origin form submit that
  // redirects back to the onboard page on success).
  let tenantId: string
  try {
    ;({ tenantId } = await requireOwnerSession())
  } catch (err) {
    if (err instanceof OwnerUnauthorizedError) {
      return NextResponse.redirect(
        new URL('/team/login?next=/team/onboard', request.url),
        { status: 303 },
      )
    }
    throw err
  }

  if (!tenantId) {
    return NextResponse.json(
      { ok: false, reason: 'tenant_not_configured' },
      { status: 503 },
    )
  }

  const form = await request.formData()
  const rawAnswer = form.get('answer')
  if (typeof rawAnswer !== 'string') {
    return NextResponse.json(
      { ok: false, reason: 'answer_missing' },
      { status: 400 },
    )
  }
  const answer = rawAnswer.trim()
  if (!answer) {
    return NextResponse.json(
      { ok: false, reason: 'answer_empty' },
      { status: 400 },
    )
  }
  if (answer.length > 4000) {
    return NextResponse.json(
      { ok: false, reason: 'answer_too_long' },
      { status: 400 },
    )
  }

  const result = await forwardOnboardStep(tenantId, answer)
  if (!result.ok) {
    // Forward failure — log + redirect back so the owner can retry.
    console.error('forwardOnboardStep failed', { reason: result.reason })
  }
  redirect('/team/onboard')
}
