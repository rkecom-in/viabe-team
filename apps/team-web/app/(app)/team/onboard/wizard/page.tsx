/**
 * VT-267 PR-C — owner onboarding wizard.
 *
 * Renders inside the WhatsApp in-app browser (full-page, no popups). Steps: review the
 * draft business_profile (edit states) → connect Google Sheets + WhatsApp (OAuth hands off to
 * the SYSTEM browser, then re-check to resume). Auth: owner session (requireFazal); tenant
 * resolved server-side (FAZAL_TENANT_ID, Phase-1).
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireFazal } from '@/lib/auth/require-fazal'
import { OnboardingWizard } from '@/components/onboard/onboarding-wizard'
import { checkConnection, type ConnectionStatus } from '@/lib/onboard/connect'
import { fetchProfileDraft, type ProfileDraft } from '@/lib/onboard/profile'

export const dynamic = 'force-dynamic'

export default async function OnboardWizardPage() {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/onboard/wizard')
    throw err
  }

  const tenantId = process.env.FAZAL_TENANT_ID ?? ''
  if (!tenantId) {
    return (
      <main data-area="onboard-wizard-degraded" className="p-6">
        <h1 className="text-2xl font-semibold">Onboarding unavailable</h1>
        <p>
          <code>FAZAL_TENANT_ID</code> is not configured.
        </p>
      </main>
    )
  }

  let draft: ProfileDraft
  let sheets: ConnectionStatus
  let whatsapp: ConnectionStatus
  try {
    ;[draft, sheets, whatsapp] = await Promise.all([
      fetchProfileDraft(tenantId),
      checkConnection(tenantId, 'google_sheet'),
      checkConnection(tenantId, 'whatsapp'),
    ])
  } catch (err) {
    return (
      <main data-area="onboard-wizard-error" className="p-6">
        <h1 className="text-2xl font-semibold">Onboarding</h1>
        <p data-section-error>couldn&apos;t load: {err instanceof Error ? err.message : 'unknown'}</p>
      </main>
    )
  }

  return (
    <main data-area="onboard-wizard" className="p-4 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">Set up your business</h1>
      </header>
      <OnboardingWizard
        draft={draft}
        initialSheets={sheets.connected}
        initialWhatsapp={whatsapp.connected}
      />
    </main>
  )
}
