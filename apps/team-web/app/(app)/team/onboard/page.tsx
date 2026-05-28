/** Integration Agent onboarding page (VT-211). */

import { redirect } from 'next/navigation'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import {
  fetchFazalOnboardState,
  TenantNotConfiguredError,
} from '@/lib/onboard/data-access'

export const dynamic = 'force-dynamic'

const PHASE_LABELS: Record<string, string> = {
  phase_1_discovery: 'Discovery — let me know what you sell',
  phase_2_auth: 'Authentication — connect a data source',
  phase_3_sample_pull: 'Sample pull — confirm the data looks right',
  phase_4_field_mapping: 'Field mapping — confirm the canonical fields',
  phase_5_confirmed: 'All set — ingestion is live',
}

export default async function OnboardPage() {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/login')
    throw err
  }

  let state
  try {
    state = await fetchFazalOnboardState()
  } catch (err) {
    if (err instanceof TenantNotConfiguredError) {
      return (
        <main data-area="onboard-degraded">
          <h1>Onboarding unavailable</h1>
          <p>
            <code>FAZAL_TENANT_ID</code> is not configured. Ask Cowork to
            populate the value from the <code>tenants</code> table.
          </p>
        </main>
      )
    }
    throw err
  }

  if (state.phase === 'phase_5_confirmed') {
    return (
      <main data-area="onboard-confirmed">
        <header>
          <h1>All set</h1>
          <p>Ingestion is live. Customer rows now flow into Viabe.</p>
        </header>
      </main>
    )
  }

  const promptText =
    state.pending_owner_input?.prompt_text ??
    'Tell me a bit about your business so I can suggest the right data sources.'

  return (
    <main data-area="onboard" data-phase={state.phase}>
      <header>
        <h1>Onboarding</h1>
        <p data-element="phase-label">{PHASE_LABELS[state.phase] ?? state.phase}</p>
      </header>

      <section data-section="agent-prompt">
        <p data-element="agent-prompt">{promptText}</p>
      </section>

      <form action="/api/onboard/answer" method="POST">
        <label>
          Your answer
          <textarea
            name="answer"
            required
            minLength={1}
            maxLength={4000}
            rows={6}
            data-element="answer-input"
          />
        </label>
        <button type="submit" data-element="submit">
          Send
        </button>
      </form>
    </main>
  )
}
