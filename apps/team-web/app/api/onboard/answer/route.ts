/** VT-211 — owner-answer ingest for the onboarding agent.
 *
 * POST handler. Validates the form input, forwards to the orchestrator's
 * /api/orchestrator/integrations/onboard-step endpoint (X-Internal-Secret
 * signed), and redirects the browser back to /team/onboard so the page
 * re-fetches state.
 */

import { redirect } from 'next/navigation'
import { NextResponse } from 'next/server'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { forwardOnboardStep } from '@/lib/orchestrator-client'

export async function POST(request: Request): Promise<Response> {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.redirect(new URL('/login', request.url), { status: 303 })
    }
    throw err
  }

  const tenantId = process.env.FAZAL_TENANT_ID ?? ''
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
